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
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import __version__
from . import capacity, ids, lanes_transfer, procs, protocol, render, scheduler
from .adapters.base import AdapterError
from .adapters.registry import get_adapter
from .contracts import (
    HEADLESS_MARKER, START_GRACE_S, TERM_GRACE_S, WAIT_POLL_MAX_S,
    Attestation, ClockSource, Closure, ClosureReason, Credential, ExitInfo,
    JobSpec, Lane, LaneOwner, Launch, Outcome, OutcomeClass, Reading,
    ReadingLabel, Sandbox, attempt_dir,
)
from .credentials import resolve_credential
from .guardian import atomic_publish
from .policy import load_policy, policy_hash, resolve_model
from .retention import maintenance
from .salvage import git_head, salvage, validate_writable_workdir
from .store import Store

TERMINAL = ("succeeded", "failed", "cancelled", "lost")

LIVE_ATTEMPTS = ("SELECT * FROM attempts WHERE state IN "
                 "('reserved','starting','running','finalizing')")

#: The sessions kit's durable facts, as `events` kinds (C-23.33, C-23.35). They
#: are events rather than a table because each is an append-only record of one
#: operator or worker decision, and the latest row for a session is the answer.
NUDGE_EVENT = "session.nudged"
RETIRE_EVENT = "session.retired"
UNRETIRE_EVENT = "session.unretired"

#: `store.transaction` writes its own audit event under the kind it is given,
#: and `_session_events` reads the NEWEST row of each kind. Naming the audit
#: event after the record would therefore shadow the record with a summary that
#: carries no dedupe key — so the two are deliberately different kinds.
def audit_kind(event: str) -> str:
    return event.rsplit(".", 1)[0] + ".recorded"

#: C-23.55: one live revive per session. The key is session-scoped, not
#: caller-scoped, and its holder is the revive job id so every existing
#: holder-keyed release site frees it. It shares the `session:` namespace with
#: C-6.5's writable-job lease, which is deliberate — both are about one session
#: having one writer — but nothing reads that namespace by prefix.
def revive_lease_key(session_id: str) -> str:
    return f"session:{session_id}:revive"


def imported_external(attempt: dict) -> bool:
    """docs/migration.md principle 3: this attempt belongs to a v1 run, not to v2.

    A v1 run still running at import time is recorded as a live attempt carrying
    `imported_external` (subfleet/importer.py), and "v2 never adopts, kills, or
    finalizes it". Recovery therefore skips it: without this the first control
    tick would contain, kill or lose a run v1 is still executing. The parse is
    defensive because a control loop that raises stops recovering everything.
    """
    try:
        return bool(json.loads(attempt.get("evidence_json") or "{}").get("imported_external"))
    except (TypeError, ValueError):
        return False
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
        self._pending_launches: set[str] = set()
        self._export_locks: dict[str, threading.Lock] = {}
        self._census_next: dict[str, float] = {}
        self._last_maintenance = time.monotonic()
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
        self._lock_finalizer = weakref.finalize(self, os.close, self._lock_fd)
        try:
            ident = {"pid": os.getpid(), "boot_id": procs.boot_id(),
                     "proc_start": procs.proc_start(os.getpid()), "version": __version__}
            if not ident["proc_start"]:
                raise procs.InspectionError("daemon identity is absent")
        except BaseException:
            self._lock_finalizer()
            raise
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
        from .timers import Timers
        self.timers = Timers(self.store, self.root, self.policy, turn=self._timer_turn,
                             deliver=self._timer_notice)
        self._recovery_complete = threading.Event()

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

    def _capacity_view(self, desktop_account=None):
        view = capacity.build_view(
            self.store.lane_rows(), self.store.list_readings(), self.store.list_closures(),
            self.store.list_attempts(), self.store.query("SELECT * FROM jobs ORDER BY created_at,rowid"),
            reading_ttl_s=self.policy["caps"]["reading_ttl_s"], desktop_account=desktop_account)
        # Probe reservations are explicit leases, not invented in-flight attempt
        # counts. A recovered probe keeps its lane unavailable until containment.
        leases = self.store.query("SELECT * FROM leases WHERE holder LIKE 'probe:%'")
        view["unavailable_lanes"] = {row["lease_key"].split(":")[1]: row["holder"] for row in leases}
        view["reserved_probes"] = len(leases)
        for lane in view["lanes"]:
            if holder := view["unavailable_lanes"].get(lane["lane_id"]):
                lane["probe_state"] = (self._probe_record(holder) or {}).get("state", "uncertain")
        return self.timers.enrich_view(view)

    def _pick(self, job: dict, *, extra_exclusions: tuple[str, ...] = (), desktop_account=None):
        # C-6.3, C-11: one pure evaluation uses the same transaction's attempt
        # rows as reservation. Desktop file I/O happens before entering it.
        exclusions = job.get("exclusions") or ()
        if isinstance(exclusions, str):
            exclusions = json.loads(exclusions)
        view = self._capacity_view(desktop_account)
        overrides = {lane["lane_id"] for lane in view["lanes"]
                     if self.timers.actions.confirmed_override(lane["lane_id"])}
        view["readings"] = [row for row in view["readings"] if row["lane_id"] not in overrides]
        return scheduler.evaluate(self.policy, view,
                                  {**job, "exclusions": tuple(exclusions) + extra_exclusions,
                                   "policy_hash": self.policy_digest})

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
                    model = resolve_model(self.policy, model)
                if args.task and args.task not in self.policy["chains"]:
                    raise ValueError(f"unknown task {args.task}")
                if args.tier and args.tier not in self.policy["tiers"]:
                    raise ValueError(f"unknown tier {args.tier}")
                if not model and not args.task and not args.pinned_lane:
                    raise ValueError("submit requires pinned_model, pinned_lane or task")
                lane_row = scheduler.resolve_lane(self.store.lane_rows(), args.pinned_lane) if args.pinned_lane else None
                lane = self.store.get_lane(lane_row["lane_id"]) if lane_row else None
                if args.pinned_lane and not lane:
                    raise ValueError(f"unknown lane {args.pinned_lane}")
                task_model = model or (self.policy["chains"][args.task][self.policy["tiers"].index(args.tier or "standard")]
                                       if args.task else next((k for k, v in self.policy["models"].items() if v["provider"] == lane.provider), None))
                if task_model is None:
                    raise ValueError(f"pinned_lane: policy has no model for provider {lane.provider}")
                provider = self.policy["models"][task_model]["provider"]
                if lane and lane.provider != provider:
                    raise ValueError("pinned lane and model providers disagree")
                if lane:
                    self._validate_home(lane)
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
                return {"dry_run": True, "decision": dataclasses.asdict(self._pick(values, desktop_account=capacity.read_desktop_account()))}
            self._validate_conflicts(values)
            jobdir.mkdir(mode=0o700)
            self._publish("prompt", jobdir / "prompt.md", prompt)
            manifest = {"job": values}
            if sandbox == Sandbox.WORKSPACE_WRITE and not args.no_preamble:
                prepared_path = jobdir / "prompt.prepared.md"
                self._publish("prompt-prepared", prepared_path, WRITE_PREAMBLE.encode() + prompt)
                manifest["prepared_prompt_path"] = str(prepared_path)
            self._publish("manifest", jobdir / "manifest.json", json_bytes(manifest))
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
        if job.get("kind") == "revive" and job.get("caller_session"):
            # C-23.55: one live revive per session. The lease below is taken in
            # the admission transaction, but admission treats a lease conflict as
            # a wait, and a revive that waits for its own twin is exactly the
            # 2026-09-04 incident dressed as patience. Refuse at submit instead,
            # so the second attempt is skipped rather than queued (C-6.5).
            session = job["caller_session"]
            lease = self.store.one("SELECT holder FROM leases WHERE lease_key=?",
                                   (revive_lease_key(session),))
            live = self.store.one(
                "SELECT job_id FROM jobs WHERE kind='revive' AND caller_session=? "
                "AND state NOT IN ('succeeded','failed','cancelled','lost')", (session,))
            if lease or live:
                holder = (lease or {}).get("holder") or (live or {}).get("job_id")
                raise AdapterError(
                    f"session {session} already has a live revive ({holder})", code=7,
                    fix=f"subfleet runs show {holder}, or kill it before reviving again")
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

    @staticmethod
    def _validate_home(lane: Lane) -> None:
        if lane.provider != "codex" or lane.credential.kind != "home":
            return
        path = Path(lane.credential.ref).expanduser() / "auth.json"
        if path.is_file():
            auth = json.loads(path.read_bytes())
            if auth.get("OPENAI_API_KEY") or auth.get("auth_mode") in ("api_key", "apikey"):
                raise AdapterError("API-key home refused", fix="log this lane into a subscription account")

    @staticmethod
    def _guard_override(adapter, lane: Lane, workdir: str) -> str | None:
        # The Codex adapter lane exposes its binary as codex_bin. Registered
        # fake adapters launch their Python fixtures and do not expose it.
        binary = getattr(adapter, "codex_bin", None)
        if lane.provider != "codex" or binary is None:
            return None
        try:
            from .guard.preflight import preflight
        except ImportError:
            raise AdapterError("Codex guard preflight is not installed", code=7,
                               fix="install the Codex adapter and reviewed guard files") from None
        result = preflight(binary, home=lane.home or lane.credential.ref, workdir=workdir)
        if not result.ok or not result.override:
            raise AdapterError(result.message, code=7, fix=result.fix or "rerun subfleet doctor")
        return result.override

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
            a = protocol.coerce_args(protocol.LanesArgs, args)
            if a.action == "transfer":
                # Ownership changes only here, and it records an event (C-10.4,
                # plan amendment 8). The roster edits belong to the daemon
                # because the daemon owns the store (C-3.4).
                try:
                    result = lanes_transfer.transfer(
                        self.store, self.root, a.lane_id, a.owner,
                        dry_run=bool(a.dry_run), confirm_v1_edit=bool(a.confirm_v1_edit))
                except lanes_transfer.TransferError as exc:
                    raise protocol.ProtocolError(str(exc), exc.code, exc.fix) from None
                return {"transfer": result,
                        "lanes": self.store.query("SELECT * FROM lanes ORDER BY lane_id")}
            return {"lanes": self._capacity_view(capacity.read_desktop_account())["lanes"],
                    "leases": self.store.query("SELECT * FROM leases WHERE lease_key LIKE 'lane:%'")}
        if op == "readings":
            view = self._capacity_view(capacity.read_desktop_account())
            return {"readings": view["readings"], "closures": view["closures"], "status": render.status(view)}
        if op == "why":
            a = protocol.coerce_args(protocol.WhyArgs, args)
            if a.job_id:
                self._job(a.job_id)
                row = self.store.one("SELECT decision_json FROM decisions WHERE job_id=? ORDER BY decision_id DESC LIMIT 1", (a.job_id,))
                decision = json.loads(row["decision_json"]) if row else None
            else:
                decision = dataclasses.asdict(self._pick(dataclasses.asdict(a), desktop_account=capacity.read_desktop_account()))
            return {"decision": decision, "text": render.why(decision) if decision else "No decision recorded."}
        if op.startswith("notice."):
            a = protocol.coerce_args(
                protocol.NoticeMarkArgs if op == "notice.mark" else protocol.NoticeArgs,
                args)
            if op == "notice.ack":
                with self.store.transaction("notice.acknowledged") as tx:
                    for notice_id in a.notice_ids:
                        if notice_id < 0:
                            tx.execute("UPDATE service_notices SET state='acknowledged',acknowledged_at=? WHERE notice_id=? AND session_id=?",
                                       (utcnow(), -notice_id, a.session_id))
                            continue
                        tx.execute("UPDATE notices SET state='acknowledged',acknowledged_at=? WHERE notice_id=? AND session_id=? AND state!='acknowledged'", (utcnow(), notice_id, a.session_id))
            if op == "notice.mark":
                # C-15.3's non-terminal states, for the delivery layers that are
                # not an acknowledgement: `offered` (a transport accepted the
                # bytes) and `surfaced` (a hook printed it). Neither may overwrite
                # `acknowledged`, which is terminal.
                if a.state not in ("offered", "surfaced", "acknowledged"):
                    raise protocol.ProtocolError(f"unknown notice state {a.state!r}")
                stamp = utcnow()
                with self.store.transaction("notice." + a.state) as tx:
                    for notice_id in a.notice_ids:
                        tx.execute(
                            "UPDATE notices SET state=?,transport=COALESCE(?,transport),"
                            "offered_at=COALESCE(offered_at,?),"
                            "acknowledged_at=CASE WHEN ?='acknowledged' THEN ? ELSE acknowledged_at END "
                            "WHERE notice_id=? AND session_id=? AND state!='acknowledged'",
                            (a.state, a.transport, stamp, a.state, stamp, notice_id, a.session_id))
            notices = self.store.query("SELECT * FROM notices WHERE session_id=? AND state IN ('pending','offered') ORDER BY notice_id", (a.session_id,))
            notices += [{**row, "notice_id": -row["notice_id"], "job_id": None} for row in
                        self.store.query("SELECT * FROM service_notices WHERE session_id=? AND state IN ('pending','offered') ORDER BY notice_id", (a.session_id,))]
            return {"notices": notices}
        if op == "ping":
            text = args.get("text", "")
            session = args.get("session_id") or self.policy.get("alerts", {}).get("operator_session") or "operator"
            notice_id = None
            if text:
                with self.store.transaction("notice.pending", data={"session_id": session}) as tx:
                    cursor = tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) VALUES(?,?,'pending',?)",
                                        (session, text, utcnow()))
                    notice_id = -cursor.lastrowid
                self._notify()
            return {"pong": True, "version": __version__, "session_id": session, "text": text, "notice_id": notice_id}
        if op == "sessions":
            return self.sessions(protocol.coerce_args(protocol.SessionsArgs, args))
        if op == "daemon.status":
            view = self._capacity_view(capacity.read_desktop_account())
            return {**view, "status": render.status(view), "pid": os.getpid(), "version": __version__, "state_root": str(self.root),
                    "timers": self.timers.status(), "active_attempts": self.store.one("SELECT count(*) n FROM attempts WHERE state IN ('reserved','starting','running','finalizing')")["n"]}
        raise protocol.ProtocolError(f"unknown op {op}")

    # --- the sessions kit's store seam (C-23.33, C-23.35, C-23.55) ------------

    def _session_events(self, kinds: tuple[str, ...],
                        session_ids: set[str] | None) -> dict[str, dict]:
        """The newest event of each kind per session id, keyed `<kind>:<id>`."""
        marks = ",".join("?" for _ in kinds)
        latest: dict[str, dict] = {}
        for row in self.store.query(
                f"SELECT kind,ts,data_json FROM events WHERE kind IN ({marks}) "
                "ORDER BY event_id DESC", kinds):
            try:
                data = json.loads(row["data_json"])
            except (TypeError, ValueError):
                continue
            session = data.get("session_id")
            if not isinstance(session, str) or (session_ids is not None
                                                and session not in session_ids):
                continue
            latest.setdefault(f"{row['kind']}:{session}", {**data, "at": row["ts"]})
        return latest

    def _lane_session_ids(self) -> list[str]:
        """Every session id subfleet itself launched as a headless lane (C-23.31)."""
        return sorted({row["native_session_id"] for row in self.store.query(
            "SELECT DISTINCT native_session_id FROM attempts "
            "WHERE native_session_id IS NOT NULL") if row["native_session_id"]})

    def sessions(self, args: protocol.SessionsArgs) -> dict:
        action = args.action or "state"
        if action == "state":
            wanted = {s for s in args.session_ids if isinstance(s, str) and s} or None
            latest = self._session_events((NUDGE_EVENT, RETIRE_EVENT, UNRETIRE_EVENT), wanted)
            leases = {row["lease_key"]: row["holder"] for row in
                      self.store.query("SELECT lease_key,holder FROM leases "
                                       "WHERE lease_key LIKE 'session:%:revive'")}
            state: dict[str, dict] = {}
            for session in sorted(wanted or {key.split(":", 1)[1] for key in latest}):
                retired = latest.get(f"{RETIRE_EVENT}:{session}")
                cleared = latest.get(f"{UNRETIRE_EVENT}:{session}")
                # Retirement is durable until the operator clears it, and both
                # halves are append-only, so the later row wins (C-23.35).
                if retired and cleared and cleared["at"] >= retired["at"]:
                    retired = None
                state[session] = {
                    "retired": retired,
                    "last_nudge": latest.get(f"{NUDGE_EVENT}:{session}"),
                    "revive_holder": leases.get(revive_lease_key(session)),
                }
            return {"sessions": state, "lane_sessions": self._lane_session_ids()}
        if action in ("retire", "unretire"):
            if not args.session_id:
                raise protocol.ProtocolError(f"sessions {action}: session_id is required")
            kind = RETIRE_EVENT if action == "retire" else UNRETIRE_EVENT
            data = {"session_id": args.session_id, "reason": args.reason, **args.detail}
            with self.store.transaction(audit_kind(kind), data=data) as tx:
                tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                           (utcnow(), kind, json.dumps(data, sort_keys=True)))
            return {"session_id": args.session_id, "action": action, "recorded": True}
        if action == "nudged":
            if not args.session_id:
                raise protocol.ProtocolError("sessions nudged: session_id is required")
            # C-23.33's dedupe and cooldown are re-checked HERE, inside the
            # transaction that records the nudge, so two sweeps racing over one
            # session cannot both reserve it. The worker has already decided
            # eligibility against the transcript (C-23.34); this is the lock.
            with self.store.transaction(audit_kind(NUDGE_EVENT),
                                        data={"session_id": args.session_id}) as tx:
                previous = self._session_events((NUDGE_EVENT,), {args.session_id}).get(
                    f"{NUDGE_EVENT}:{args.session_id}")
                if previous and not args.force:
                    if args.dedupe_key and previous.get("dedupe_key") == args.dedupe_key:
                        return {"recorded": False, "session_id": args.session_id,
                                "reason": "already nudged at this interruption point",
                                "last_nudge": previous}
                    cooldown = args.cooldown_s
                    if cooldown and previous.get("at"):
                        elapsed = age(previous["at"])
                        if elapsed < float(cooldown):
                            return {"recorded": False, "session_id": args.session_id,
                                    "reason": (f"nudged {int(elapsed)}s ago "
                                               f"(cooldown {int(float(cooldown))}s)"),
                                    "last_nudge": previous}
                data = {"session_id": args.session_id, "dedupe_key": args.dedupe_key,
                        "kind": args.kind, **({"forced": True} if args.force else {}),
                        **args.detail}
                tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                           (utcnow(), NUDGE_EVENT, json.dumps(data, sort_keys=True)))
            return {"recorded": True, "session_id": args.session_id,
                    "dedupe_key": args.dedupe_key, "kind": args.kind}
        raise protocol.ProtocolError(f"unknown sessions action {action!r}")

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
                for job in jobs:
                    job["attempt"] = self.store.one(
                        "SELECT * FROM attempts WHERE job_id=? ORDER BY seq DESC LIMIT 1", (job["job_id"],))
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

    def _control(self) -> None:
        # Recovery uses the same idempotent workers as normal execution. A
        # reserved row absent from this process's launch set was never granted
        # permission to run by this daemon instance.
        while not self.stopping.is_set():
            try:
                for a in self.store.query(LIVE_ATTEMPTS):
                    if imported_external(a):
                        continue                    # v1 still owns it (principle 3)
                    self._schedule(a["attempt_id"], self._process_attempt, a["attempt_id"])
                for j in self.store.query("SELECT * FROM jobs WHERE accepted_attempt_id IS NOT NULL"):
                    if self.store.one("SELECT 1 FROM leases WHERE holder=?", (j["job_id"],)):
                        self._schedule("export:" + j["job_id"], self._export, j["job_id"])
                if self._recovery_complete.is_set():
                    self._schedule("admission", self._admit)
                    self.timers.tick()
                else:
                    self._schedule("timer-recovery", self._recover_then_start_timers)
                if time.monotonic() - self._last_maintenance >= 3600:
                    self._last_maintenance = time.monotonic()
                    self._schedule("retention", self._retention)
            except Exception as exc:
                self.log.error("control iteration failed: %s", type(exc).__name__)
            self.stopping.wait(self.tick_s)

    def _timer_notice(self, notice: dict) -> bool:
        result = self.dispatch("ping", {"session_id": self.policy.get("alerts", {}).get("operator_session"),
                                       "text": notice["subject"] + "\n" + notice["body"]})
        return result.get("notice_id") is not None

    def _retention(self):
        try:
            result = maintenance(self.store, self.root, cancel=self.timers.cancel, deadline=time.monotonic() + 60)
            if result.get("interrupted"):
                self.timers.mark("retention", error="CancelledError" if result["interrupted"] == "cancelled" else "TimeoutError", next_due=after(3600))
                return
            with self.store.transaction("service-notice.retention") as tx:
                tx.execute("DELETE FROM service_notices WHERE state IN ('acknowledged','surfaced') AND created_at<?", (after(-14 * 86400),))
        except Exception as exc:
            self.timers.mark("retention", error=type(exc).__name__, next_due=after(3600))
            raise
        self.timers.mark("retention", next_due=after(3600))

    def _recover_then_start_timers(self):
        # HTTP reservations have no provider process and can be released on restart.
        for lease in self.store.query("SELECT * FROM leases WHERE holder LIKE 'probe:timer:%'"):
            if not self._probe_record(lease["holder"]):
                self.store.release_leases(lease["holder"])
        self._recover_probes()
        self.timers.actions.recover()
        self.timers.start()
        self._recovery_complete.set()

    def _timer_turn(self, lane: Lane, purpose: str, holder: str, *, cancel, deadline) -> Outcome:
        if cancel.is_set() or time.monotonic() >= deadline:
            return Outcome(OutcomeClass.UNKNOWN, "timer cancelled", evidence={"timed_out": True})
        token = holder.rsplit(":", 1)[-1]
        directory = self.root / "lanes" / lane.lane_id / "probes" / token
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        model = self.policy["models"]["haiku" if lane.provider == "claude" else "terra"]
        record = {"holder": holder, "job_id": None, "lane_id": lane.lane_id,
                  "timer_kind": purpose, "model_id": model["id"], "directory": str(directory),
                  "state": "reserved", "created_at": utcnow(), "owned_identities": {},
                  "deadline_at": after(max(0, deadline - time.monotonic()))}
        self._save_probe(record)
        job = {"job_id": "timer-" + token, "request_id": token, "kind": "probe",
               "workdir": str(directory), "prompt_path": str(directory / "prompt.md"),
               "sandbox": "read-only", "exclusions": "[]",
               # _spec builds a JobSpec from a job row; a timer turn has no routing fields.
               "task": None, "tier": None, "pinned_model": None, "pinned_lane": None,
               "name": None, "out_path": None}
        try:
            outcome = self._execute_probe(job, lane, model, holder)
        except Exception:
            current = self._probe_record(holder)
            safe = self._contain_probe(current)
            if not safe:
                return Outcome(OutcomeClass.UNKNOWN, "timer quarantined", evidence={"probe_quarantined": True})
            raise
        requested = (self._read_json(directory / "request.json") or {}).get("requested_at")
        if requested:
            self.store.add_event("timer.request", lane_id=lane.lane_id,
                                 data={"requested_at": requested, "purpose": purpose,
                                       "rc": outcome.evidence.get("rc"), "native_session_id": outcome.native_session_id})
        evidence = {**outcome.evidence, "requested_at": requested,
                    "timed_out": time.monotonic() >= deadline}
        if not evidence.get("probe_quarantined"):
            record = self._probe_record(holder)
            record.update(state="completed")
            self._save_probe(record)
            shutil.rmtree(directory, ignore_errors=True)
        return dataclasses.replace(outcome, evidence=evidence)

    def _workspace(self, job: dict) -> tuple[str, str | None, str | None]:
        workdir = job.get("worktree") or job["workdir"]
        if job["sandbox"] == "workspace-write" and not job["in_place"] and not job.get("worktree"):
            workdir = str(self.root / "worktrees" / job["job_id"])
            if not Path(workdir).exists():
                result = subprocess.run(["git", "-C", job["workdir"], "worktree", "add", "--detach", workdir, job["workdir_head"]],
                                        capture_output=True, timeout=30)
                if result.returncode:
                    raise AdapterError("could not allocate worktree", fix="check repository and state-root permissions")
            os.chmod(workdir, 0o700)
        head = git_head(workdir)
        baseline = None
        if head:
            result = subprocess.run(["git", "-C", workdir, "rev-parse", f"{head}^{{tree}}"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                baseline = result.stdout.strip()
        return workdir, head, baseline

    def _probe_record(self, holder: str) -> dict | None:
        # C-8.4: probe state and results live in events, never synthetic jobs.
        rows = self.store.query("SELECT data_json FROM events WHERE kind='probe.state' ORDER BY event_id DESC")
        for row in rows:
            record = json.loads(row["data_json"])
            if record.get("holder") == holder:
                return record
        return None

    def _save_probe(self, record: dict) -> None:
        self.store.add_event("probe.state", job_id=record["job_id"], lane_id=record["lane_id"], data=record)

    def _probe_census(self, record: dict):
        return procs.containment(record.get("pgid"), record.get("guardian_pid"),
                                 record.get("child_pid"), record["holder"], root=str(self.root))

    def _contain_probe(self, record: dict) -> bool:
        """C-5.4–7: terminate only recorded identities and retain uncertain leases."""
        census = self._probe_census(record)
        owned = {int(pid): procs.ProcessIdentity(**value)
                 for pid, value in record.get("owned_identities", {}).items()}
        pid = record.get("guardian_pid")
        leader_live = pid and procs.same_process(pid, record.get("boot_id"), record.get("proc_start"))
        if leader_live:
            owned.update({p: ident for p, ident in census.identities.items() if p in census.group_pids})
            owned[pid] = procs.ProcessIdentity(pid, record["boot_id"], record["proc_start"])
        record["owned_identities"] = {str(p): dataclasses.asdict(ident) for p, ident in owned.items()}
        if not census.verified_empty and record.get("state") != "quarantined":
            record["state"] = "containing"
            self._save_probe(record)  # Authority precedes every signal, including recovery.
            if leader_live:
                procs.signal_group(record["pgid"], signal.SIGTERM,
                                   boot_id=record["boot_id"], proc_start=record["proc_start"])
            deadline = time.monotonic() + self.term_grace_s
            while not census.verified_empty and time.monotonic() < deadline:
                time.sleep(.05)
                census = self._probe_census(record)
            if not census.verified_empty:
                if leader_live:
                    procs.signal_group(record["pgid"], signal.SIGKILL,
                                       boot_id=record["boot_id"], proc_start=record["proc_start"])
                for target in census.live_pids:
                    if target in owned:
                        procs.signal_process(owned[target], signal.SIGKILL)
                time.sleep(.05)
                census = self._probe_census(record)
        record.update(state="contained" if census.verified_empty else "quarantined",
                      containment=census.to_dict())
        self._save_probe(record)
        if not census.verified_empty:
            with self.store.transaction("probe.quarantined", job_id=record["job_id"],
                                        lane_id=record["lane_id"], data={"containment": census.to_dict()}) as tx:
                tx.execute("UPDATE jobs SET state='waiting',wait_reason='uncertain',next_check_at=? WHERE job_id=? AND state IN ('queued','waiting')",
                           (after(60), record["job_id"]))
        return census.verified_empty

    def _await_probe(self, record: dict, child=None) -> tuple[bool, dict | None]:
        """Re-adopt the same gated guardian, bounded by its durable deadline."""
        directory = Path(record["directory"])
        next_census = 0.0
        while not self.stopping.is_set():
            if child:
                child.poll()  # Reap our own guardian when it finishes.
            receipt = self._read_json(directory / "exit.json")
            if receipt:
                record["child_pid"] = receipt.get("child_pid")
                break
            job = self.store.get_job(record["job_id"])
            if not record.get("timer_kind") and (not job or job["cancel_requested_at"] or job["state"] in TERMINAL):
                break
            if record.get("timer_kind") and self.timers.cancel.is_set():
                break
            if record.get("state") in ("reserved", "quarantined", "containing", "contained"):
                break
            if record["deadline_at"] <= utcnow() or not procs.same_process(
                    record["guardian_pid"], record["boot_id"], record["proc_start"]):
                break
            if time.monotonic() >= next_census:
                census = self._probe_census(record)
                if not procs.same_process(record["guardian_pid"], record["boot_id"], record["proc_start"]):
                    break
                owned = dict(record.get("owned_identities", {}))
                owned.update({str(pid): dataclasses.asdict(ident) for pid, ident in census.identities.items()
                              if pid in census.group_pids})
                if owned != record.get("owned_identities", {}):
                    record["owned_identities"] = owned
                    self._save_probe(record)
                next_census = time.monotonic() + .5
            self.stopping.wait(.05)
        safe = self._contain_probe(record)
        if child:
            child.poll()
        return safe, self._read_json(directory / "exit.json")

    def _execute_probe(self, job: dict, lane: Lane, model: dict, holder: str) -> Outcome:
        """C-11.4, C-5.1: run a read-only requested-model probe through the guardian.

        This is the process seam for tests. A normal return guarantees containment
        unless the durable probe record is quarantined; no secret enters a receipt.
        """
        record = self._probe_record(holder)
        directory = Path(record["directory"])
        adapter = get_adapter(lane.provider)
        self._validate_home(lane)
        credential_env = resolve_credential(lane.credential)
        prompt = directory / "prompt.md"
        self._publish("probe-prompt", prompt, b"Reply with exactly OK. Do not use tools.\n")
        spec = self._spec(job, kind="probe", workdir=str(directory), prompt_path=str(prompt),
                          sandbox=Sandbox.READ_ONLY, out_path=None)
        launch = adapter.build_launch(spec, holder, directory, lane, credential_env,
                                      model["id"], model.get("effort"), prompt,
                                      self._guard_override(adapter, lane, str(directory)))
        safe_launch = dataclasses.asdict(launch)
        safe_launch.pop("env_add")
        self._publish("probe-launch", directory / "launch.json", json_bytes(safe_launch))
        env = {**os.environ, **launch.env_add}
        for key in (*launch.env_remove, "CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            env.pop(key, None)
        env.update(SUBFLEET_JOB=job["job_id"], SUBFLEET_ATTEMPT=holder, SUBFLEET_ROOT=str(self.root), SUBFLEET_PROBE="1")  # C-5.1, C-11.4
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        read_fd, write_fd = os.pipe()
        command = [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(directory),
                   "--cwd", launch.cwd, "--stdout-path", launch.stdout_path,
                   "--stderr-path", launch.stderr_path, "--launch-fd", str(read_fd)]
        if launch.stdin_path:
            command += ["--stdin-path", launch.stdin_path]
        command += ["--", *launch.argv]
        child = None
        try:
            child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     pass_fds=(read_fd,), close_fds=True, cwd=package_root)
            identity_deadline = time.monotonic() + 2
            started = procs.proc_start(child.pid)
            while not started and child.poll() is None and time.monotonic() < identity_deadline:
                time.sleep(.01)
                started = procs.proc_start(child.pid)
            if not started:
                raise procs.InspectionError("probe guardian identity is absent")
            record.update(state="starting", guardian_pid=child.pid, pgid=child.pid,
                          boot_id=procs.boot_id(), proc_start=started)
            self._save_probe(record)
            if not record.get("timer_kind") or not self.timers.cancel.is_set():
                os.write(write_fd, b"1")  # Committed ownership is required to open the gate.
        except (OSError, procs.InspectionError) as exc:
            record["launch_error"] = type(exc).__name__
        finally:
            os.close(read_fd)
            os.close(write_fd)
        if child is None:
            return Outcome(OutcomeClass.UNKNOWN, "probe guardian could not be spawned")
        safe, receipt = self._await_probe(record, child)
        if not safe:
            return Outcome(OutcomeClass.UNKNOWN, "probe containment is quarantined",
                           evidence={"probe_quarantined": True})
        if not receipt:
            return Outcome(OutcomeClass.UNKNOWN, "probe ended without an exit receipt")
        outcome = adapter.classify(directory, launch, ExitInfo(**{key: receipt.get(key) for key in
                                ("rc", "signal", "wall_s", "child_pid", "spawn_error")}))
        request = self._read_json(directory / "request.json") or {}
        return dataclasses.replace(outcome, evidence={**outcome.evidence, **request,
                                   "rc": receipt.get("rc"), "signal": receipt.get("signal")})

    def _finish_probe(self, record: dict, outcome: Outcome) -> None:
        if outcome.cls == OutcomeClass.LIMITED and outcome.closure is None:
            outcome = dataclasses.replace(outcome, closure=Closure(
                record["lane_id"], record["model_id"], after(3600), ClosureReason.PROVIDER_LIMIT,
                ClockSource.GUESSED, None))
        if record.get("timer_kind"):
            sent = (self._read_json(Path(record["directory"]) / "request.json") or {}).get("requested_at")
            if sent:
                self.store.add_event("timer.request", lane_id=record["lane_id"], data={"requested_at": sent, "rc": outcome.evidence.get("rc"),
                                                                      "native_session_id": outcome.native_session_id})
            if record["timer_kind"] == "keepalive" and sent and outcome.cls == OutcomeClass.OK:
                self.store.add_reading(Reading(record["lane_id"], record["model_id"], "admission", None, None,
                                              ReadingLabel.ADMISSION_OBSERVED, "keepalive", sent))
            record.update(state="completed")
            self._save_probe(record)
            self.store.release_leases(record["holder"])
            shutil.rmtree(record["directory"], ignore_errors=True)
            return
        with self.store.transaction("probe.completed", job_id=record["job_id"], lane_id=record["lane_id"],
                                    data={"model": record["model_id"], "class": outcome.cls.value,
                                          "evidence": outcome.evidence}) as tx:
            if outcome.cls == OutcomeClass.AUTH_DEAD:
                self.store.update_lane(record["lane_id"], enabled=0)
                self.timers.record_auth_dead(record["lane_id"])
            for reading in outcome.readings:
                self.store.add_reading(dataclasses.replace(reading, attempt_id=None))
            if outcome.closure:
                self.store.add_closure(outcome.closure)
            if outcome.cls == OutcomeClass.OK:
                self.store.add_reading(Reading(record["lane_id"], record["model_id"], "admission", None, None,
                                              ReadingLabel.ADMISSION_OBSERVED, "probe", utcnow()))
            record.update(state="completed", outcome=dataclasses.asdict(outcome))
            self._save_probe(record)
            tx.execute("DELETE FROM leases WHERE holder=?", (record["holder"],))
            tx.execute("UPDATE jobs SET wait_reason='capacity',next_check_at=? WHERE job_id=? AND state='waiting' AND wait_reason='uncertain'",
                       (utcnow(), record["job_id"]))
        shutil.rmtree(record["directory"], ignore_errors=True)

    def _recover_probes(self) -> None:
        """C-5.3–7, C-8.4: recover each durable probe before admitting more work."""
        for lease in self.store.query("SELECT * FROM leases WHERE holder LIKE 'probe:%'"):
            if lease["holder"] in self.timers.active_holders:
                continue
            record = self._probe_record(lease["holder"])
            if not record:
                continue  # No recorded identity grants no authority to release or kill.
            safe, receipt = self._await_probe(record)
            if not safe:
                continue
            outcome = Outcome(OutcomeClass.UNKNOWN, "probe recovered without an exit receipt")
            if receipt and (value := self._read_json(Path(record["directory"]) / "launch.json")):
                value.update(env_add={"SUBFLEET_LANE": record["lane_id"]},
                             argv=tuple(value["argv"]), env_remove=tuple(value["env_remove"]))
                adapter = get_adapter(self.store.get_lane(record["lane_id"]).provider)
                outcome = adapter.classify(Path(record["directory"]), Launch(**value), ExitInfo(**{
                    key: receipt.get(key) for key in ("rc", "signal", "wall_s", "child_pid", "spawn_error")}))
            self._finish_probe(record, outcome)

    def _probe_candidate(self, job: dict, decision, holder: str) -> Outcome:
        lane = self.store.get_lane(decision.chosen_lane)
        model = self.policy["models"][decision.chosen_model]
        try:
            outcome = self._execute_probe(job, lane, model, holder)
        except (AdapterError, OSError, subprocess.SubprocessError) as exc:
            outcome = Outcome(OutcomeClass.UNKNOWN, f"probe unavailable: {type(exc).__name__}")
            self._contain_probe(self._probe_record(holder))
        record = self._probe_record(holder)
        if record["state"] not in ("reserved", "contained", "quarantined"):
            self._contain_probe(record)
            record = self._probe_record(holder)
        if record["state"] == "quarantined":
            return Outcome(OutcomeClass.UNKNOWN, "probe containment is quarantined",
                           evidence={"probe_quarantined": True})
        self._finish_probe(record, outcome)
        return outcome

    def _prepare_route(self, job: dict, decision_job: dict, exclusions: tuple[str, ...]):
        # The admission worker serializes probes; a distinct durable holder and
        # a gated guardian prevent a restart from launching a duplicate probe.
        approved = set()
        for _ in range(len(self.store.list_lanes()) * len(self.policy["models"]) + 1):
            desktop = capacity.read_desktop_account()
            current = self._job(job["job_id"])
            if current["cancel_requested_at"] or current["state"] in TERMINAL:
                return None, desktop
            decision = self._pick(decision_job, extra_exclusions=exclusions, desktop_account=desktop)
            pair = (decision.chosen_lane, decision.chosen_model)
            if not scheduler.probe_required(decision, job) or pair in approved:
                return approved, desktop
            token = os.urandom(12).hex()
            holder = f"probe:{token}"
            directory = self.root / "lanes" / decision.chosen_lane / "probes" / token
            directory.mkdir(mode=0o700, parents=True)
            record = {"holder": holder, "job_id": job["job_id"], "lane_id": decision.chosen_lane,
                      "model_id": self.policy["models"][decision.chosen_model]["id"],
                      "directory": str(directory), "state": "reserved", "created_at": utcnow(),
                      "deadline_at": after(60), "owned_identities": {}}
            with self.store.transaction("probe.reserved", job_id=job["job_id"], lane_id=decision.chosen_lane):
                if not self.store.acquire_lease(f"lane:{decision.chosen_lane}:slot:0", holder):
                    return None, desktop
                self._save_probe(record)
            outcome = self._probe_candidate(job, decision, holder)
            if outcome.cls == OutcomeClass.OK:
                approved.add(pair)
            elif outcome.cls != OutcomeClass.LIMITED:
                with self.store.transaction("job.probe_waiting", job_id=job["job_id"]) as tx:
                    self.store.add_decision(job["job_id"], decision)
                    tx.execute("UPDATE jobs SET state='waiting',wait_reason=?,next_check_at=? WHERE job_id=? AND state IN ('queued','waiting')",
                               ("uncertain" if outcome.evidence.get("probe_quarantined") else "capacity",
                                after(60), job["job_id"]))
                return None, desktop
        return None, desktop

    def _admit(self) -> None:
        self._recover_probes()
        desktop_account = capacity.read_desktop_account()
        queued = self.store.query("SELECT * FROM jobs WHERE state IN ('queued','waiting') AND cancel_requested_at IS NULL ORDER BY created_at,rowid")
        blocked_tiers = set()
        for job in scheduler.ordered_jobs(self.policy, queued):
            tier = job["tier"] or ("standard" if "standard" in self.policy["tiers"] else self.policy["tiers"][0])
            if job["started_at"] and age(job["started_at"]) >= job["max_wall_s"]:
                self.kill(protocol.KillArgs(job["job_id"]))
                continue
            if tier in blocked_tiers:
                continue
            if job["wait_reason"] in ("approval", "uncertain"):
                continue
            if job["next_check_at"] and job["next_check_at"] > utcnow():
                if job["wait_reason"] == "capacity":
                    blocked_tiers.add(tier)
                continue
            if self.store.one("SELECT 1 FROM attempts WHERE job_id=? AND state IN ('reserved','starting','running','finalizing','quarantined')", (job["job_id"],)):
                continue
            try:
                workspace, head, baseline = self._workspace(job)
            except (OSError, subprocess.SubprocessError, AdapterError):
                self._fail_queued(job, "workspace preparation failed")
                continue
            previous = self.store.list_attempts(job["job_id"])
            extra_exclusions = tuple(a["lane_id"] for a in previous if a["outcome_class"] == "limited")
            transient_counts: dict[str, int] = {}
            for a in previous:
                if a["outcome_class"] == "transient":
                    transient_counts[a["lane_id"]] = transient_counts.get(a["lane_id"], 0) + 1
            extra_exclusions += tuple(l for l, n in transient_counts.items() if n >= 2)
            decision_job = job
            if previous and previous[-1]["outcome_class"] == "transient" and transient_counts[previous[-1]["lane_id"]] == 1:
                decision_job = {**job, "pinned_lane": previous[-1]["lane_id"],
                                "pinned_model": previous[-1]["model_requested"]}
            approved, desktop_account = self._prepare_route(job, decision_job, extra_exclusions)
            if approved is None:
                blocked_tiers.add(tier)
                continue
            with self.store.transaction("attempt.reserved", job_id=job["job_id"]) as tx:
                job = self._job(job["job_id"])
                if job["cancel_requested_at"] or job["state"] in TERMINAL:
                    continue
                if extra_exclusions:
                    job["exclusions"] = json.dumps(sorted(set(json.loads(job["exclusions"])) | set(extra_exclusions)))
                    tx.execute("UPDATE jobs SET exclusions=? WHERE job_id=?", (job["exclusions"], job["job_id"]))
                decision = self._pick(decision_job, extra_exclusions=extra_exclusions, desktop_account=desktop_account)
                live = tx.execute("SELECT count(*) FROM attempts WHERE state IN ('reserved','starting','running','finalizing')").fetchone()[0]
                if not decision.chosen_lane or live >= self.policy["caps"]["max_active_attempts"]:
                    blocked_tiers.add(tier)
                    waiting = scheduler.waiting_metadata(decision)
                    self.store.add_decision(job["job_id"], decision)
                    tx.execute("UPDATE jobs SET state='waiting',wait_reason=?,next_check_at=? WHERE job_id=?",
                               (waiting["wait_reason"], waiting["next_check_at"], job["job_id"]))
                    continue
                if scheduler.probe_required(decision, job) and (decision.chosen_lane, decision.chosen_model) not in approved:
                    blocked_tiers.add(tier)
                    continue  # The chosen identity changed after its probe.
                seq = len(previous) + 1
                aid = ids.attempt_id(job["job_id"], seq)
                lane_id = decision.chosen_lane
                slot = 0
                while tx.execute("SELECT 1 FROM leases WHERE lease_key=?", (f"lane:{lane_id}:slot:{slot}",)).fetchone():
                    slot += 1
                leases = [(f"lane:{lane_id}:slot:{slot}", aid)]
                if job["out_path"]:
                    leases.append((f"out:{job['out_path']}", job["job_id"]))
                if job["sandbox"] == "workspace-write":
                    leases.append((f"worktree:{workspace}", job["job_id"]))
                    if job["caller_session"]:
                        leases.append((f"session:{job['caller_session']}", job["job_id"]))
                revive_key = (revive_lease_key(job["caller_session"])
                              if job["kind"] == "revive" and job["caller_session"] else None)
                if revive_key:
                    # C-23.55: the census the sweep skips on is the lease rows,
                    # read inside the admitting transaction, not a snapshot taken
                    # at the start of the pass.
                    held = tx.execute("SELECT holder FROM leases WHERE lease_key=?",
                                      (revive_key,)).fetchone()
                    if held and held[0] != job["job_id"]:
                        # Skipped, not queued: waiting for the other revive to
                        # end would launch the twin the moment it did.
                        self._skip_revive(tx, job, held[0])
                        continue
                    leases.append((revive_key, job["job_id"]))
                conflict = any((r := tx.execute("SELECT holder FROM leases WHERE lease_key=?", (key,)).fetchone()) and r[0] != holder for key, holder in leases)
                if conflict:
                    blocked_tiers.add(tier)
                    tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? WHERE job_id=?", (after(1), job["job_id"]))
                    continue
                for key, holder in leases:
                    tx.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)", (key, holder, utcnow()))
                tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,baseline_tree,evidence_json,reserved_at) VALUES(?,?,?,?,?,'reserved',?,?,?)",
                           (aid, job["job_id"], seq, lane_id, self.policy["models"][decision.chosen_model]["id"], baseline,
                            json.dumps({"baseline_commit": head, "model_short": decision.chosen_model}), utcnow()))
                tx.execute("INSERT INTO decisions(job_id,attempt_id,evaluated_at,policy_hash,decision_json) VALUES(?,?,?,?,?)",
                           (job["job_id"], aid, utcnow(), self.policy_digest, json.dumps(dataclasses.asdict(decision))))
                tx.execute("UPDATE jobs SET state='running',wait_reason=NULL,next_check_at=NULL,worktree=?,started_at=COALESCE(started_at,?) WHERE job_id=?",
                           (workspace if job["sandbox"] == "workspace-write" else None, utcnow(), job["job_id"]))
                with self._busy_lock:
                    self._busy.add(aid)
            try:
                self._boundary("reserved", job["job_id"], aid)
                self._pending_launches.add(aid)
            finally:
                with self._busy_lock:
                    self._busy.discard(aid)
            self._notify()

    def _skip_revive(self, tx, job: dict, holder: str) -> None:
        """C-23.55: a session that already holds the revive lease is skipped.

        Terminal, not queued. A revive that waits for its twin to finish would
        launch a second continuation the moment the first ended, which is the
        2026-09-04 twin with a delay. rc 7 is the refusal code (C-17.3).
        """
        tx.execute("UPDATE jobs SET state='cancelled',rc=7,wait_reason=NULL,"
                   "next_check_at=NULL,finished_at=? WHERE job_id=?",
                   (utcnow(), job["job_id"]))
        tx.execute("DELETE FROM leases WHERE holder=?", (job["job_id"],))
        self._notice(tx, job, "refused", 7, None,
                     f"skipped: session {job['caller_session']} already has a live "
                     f"revive ({holder}) holding {revive_lease_key(job['caller_session'])}")

    def _fail_queued(self, job: dict, detail: str) -> None:
        with self.store.transaction("job.failed", job_id=job["job_id"]) as tx:
            job = self._job(job["job_id"])
            if job["state"] in TERMINAL:
                return
            state, rc = ("cancelled", 130) if job["cancel_requested_at"] else ("failed", 1)
            tx.execute("UPDATE jobs SET state=?,rc=?,finished_at=? WHERE job_id=?", (state, rc, utcnow(), job["job_id"]))
            tx.execute("DELETE FROM leases WHERE holder=?", (job["job_id"],))
            self._notice(tx, job, "unknown", rc, None, detail)
        self._notify()

    def _launch(self, a: dict) -> None:
        job = self._job(a["job_id"])
        self._pending_launches.discard(a["attempt_id"])
        if job["cancel_requested_at"]:
            self._unlaunched(a, "cancelled-before-launch")
            return
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        adir.mkdir(mode=0o700, exist_ok=True)
        lane = self.store.get_lane(a["lane_id"])
        adapter = get_adapter(lane.provider)
        evidence = json.loads(a["evidence_json"] or "{}")
        model = self.policy["models"][evidence["model_short"]]
        prompt_path = Path(job["prompt_path"])
        prepared_path = prompt_path.with_name("prompt.prepared.md")
        if prepared_path.is_file():
            prompt_path = prepared_path
        if a["seq"] > 1 and job["sandbox"] == "workspace-write":
            refs = self.store.query("SELECT path FROM artifacts JOIN attempts USING(attempt_id) WHERE job_id=? AND role='salvage' ORDER BY seq", (job["job_id"],))
            suffix = f"\n\nContinue from checkpoint {evidence.get('baseline_commit')}. Preserved snapshots: {', '.join(r['path'] for r in refs) or 'none'}.\n"
            prompt = prompt_path.read_bytes() + suffix.encode()
            prompt_path = adir / "prompt.md"
            self._publish("prompt", prompt_path, prompt)
        try:
            self._validate_home(lane)
            credential_env = resolve_credential(lane.credential)
            spec = self._spec(job, workdir=job.get("worktree") or job["workdir"], prompt_path=str(prompt_path))
            guard_override = self._guard_override(adapter, lane, spec.workdir)
            if job["kind"] == "revive" and job["caller_session"]:
                # C-23.54: a revive is an ordinary submission, but the launch it
                # asks for is `--resume <session id>` — continuing the session
                # named by `caller_session`, which for a revive IS the session
                # being revived. `build_launch` would start a NEW conversation
                # under a fresh `--session-id`, which looks like a revive and is
                # not one.
                launch = adapter.resume_launch(spec, a["attempt_id"], adir, lane,
                                               credential_env, job["caller_session"],
                                               prompt_path, guard_override, model["id"])
                if launch is None:
                    raise AdapterError(
                        f"{lane.provider} cannot resume a session in place", code=7,
                        fix="use `subfleet sessions handoff` to continue this session")
            else:
                launch = adapter.build_launch(spec, a["attempt_id"], adir, lane, credential_env,
                                              model["id"], model.get("effort"), prompt_path, guard_override)
        except AdapterError as exc:
            self._launch_failure(a, str(exc) + (f"; fix: {exc.fix}" if exc.fix else ""), rc=exc.code)
            return
        self._launches[a["attempt_id"]] = launch
        safe_launch = dataclasses.asdict(launch)
        safe_launch.pop("env_add")
        self._publish("launch", adir / "launch.json", json_bytes(safe_launch))
        self._publish("lane-log", adir / "lane.log", b"")
        env = dict(os.environ)
        env.update(launch.env_add)
        for key in (*launch.env_remove, "CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            env.pop(key, None)
        env.update(SUBFLEET_JOB=a["job_id"], SUBFLEET_ATTEMPT=a["attempt_id"], SUBFLEET_ROOT=str(self.root))  # C-5.1 markers
        # The package path is explicit: provider cwd is deliberately unrelated
        # to the daemon's installation or test checkout.
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        read_fd, write_fd = os.pipe()
        command = [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(adir),
                   "--cwd", launch.cwd, "--stdout-path", launch.stdout_path,
                   "--stderr-path", launch.stderr_path, "--launch-fd", str(read_fd)]
        if launch.stdin_path:
            command += ["--stdin-path", launch.stdin_path]
        if self.guardian_start_delay_s:
            command += ["--start-delay-s", str(self.guardian_start_delay_s)]
        command += ["--", *launch.argv]
        try:
            child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     pass_fds=(read_fd,), close_fds=True, cwd=package_root)
            self._children[a["attempt_id"]] = child
            # ps can miss a pid for a few milliseconds after fork, and it is
            # slow under load; retry briefly while the guardian is alive. The
            # gate byte is not written until identity is recorded (C-5.2, C-5.3).
            started = procs.proc_start_retry(child.pid, tries=8, delay_s=0.25,
                                             alive=lambda: child.poll() is None)
            if not started:
                rc = child.poll()
                raise procs.InspectionError(
                    f"guardian exited before identity (rc={rc})" if rc is not None
                    else "guardian identity is absent after 2 s")
            boot = procs.boot_id()
            with self.store.transaction("attempt.starting", job_id=a["job_id"], attempt_id=a["attempt_id"]) as tx:
                tx.execute("UPDATE attempts SET state='starting',guardian_pid=?,pgid=?,boot_id=?,proc_start=?,native_session_id=? WHERE attempt_id=? AND state='reserved'",
                           (child.pid, child.pid, boot, started, launch.native_session_id, a["attempt_id"]))
            os.write(write_fd, b"1")
        except (OSError, procs.InspectionError) as exc:
            # Closing the gate guarantees an unrecorded guardian cannot launch.
            os.close(write_fd)
            write_fd = -1
            self._unlaunched(a, f"guardian-identity-unavailable: {type(exc).__name__}: {exc}")
            return
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        self._starting_deadlines[a["attempt_id"]] = time.monotonic() + self.start_grace_s
        self._boundary("starting", a["job_id"], a["attempt_id"])

    def _launch_failure(self, a: dict, detail: str, *, rc: int = 127) -> None:
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        adir.mkdir(mode=0o700, exist_ok=True)
        self._publish("exit", adir / "exit.json", json_bytes({"rc": rc, "signal": None, "wall_s": 0, "child_pid": None,
                      "finished_at": utcnow(), "spawn_error": detail}))
        self._begin_finalizing(a, self._read_json(adir / "exit.json"))

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            return json.loads(path.read_bytes())
        except FileNotFoundError:
            return None

    def _process_attempt(self, aid: str) -> None:
        a = self.store.get_attempt(aid)
        if not a or a["state"] not in LIVE:
            return
        child = self._children.get(aid)
        if child and child.poll() is not None:
            self._children.pop(aid, None)
        if a["state"] == "reserved":
            if aid in self._pending_launches:
                self._launch(a)
            else:
                self._unlaunched(a, "reserved-no-launch")
            return
        job = self._job(a["job_id"])
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        if a["state"] == "finalizing":
            self._finalize(a)
            return
        start = self._read_json(adir / "start.json")
        receipt = self._read_json(adir / "exit.json")
        if start and a["state"] == "starting":
            with self.store.transaction("attempt.running", job_id=a["job_id"], attempt_id=aid) as tx:
                tx.execute("UPDATE attempts SET state='running',guardian_pid=?,pgid=?,boot_id=?,proc_start=?,started_at=? WHERE attempt_id=? AND state='starting'",
                           (start["guardian_pid"], start["pgid"], start["boot_id"], start["proc_start"], start["started_at"], aid))
            self._starting_deadlines.pop(aid, None)
            self._boundary("running", a["job_id"], aid)
            a = self.store.get_attempt(aid)
        if receipt:
            self._begin_finalizing(a, receipt)
            return
        if job["cancel_requested_at"] or age(job["started_at"]) >= job["max_wall_s"]:
            if not job["cancel_requested_at"]:
                with self.store.transaction("job.wall_limit", job_id=job["job_id"]) as tx:
                    tx.execute("UPDATE jobs SET cancel_requested_at=? WHERE job_id=? AND cancel_requested_at IS NULL", (utcnow(), job["job_id"]))
                    tx.execute("UPDATE attempts SET killed_by='max_wall_s' WHERE attempt_id=?", (aid,))
                a = self.store.get_attempt(aid)
            self._kill_attempt(a)
            return
        if a["state"] == "starting":
            deadline = self._starting_deadlines.setdefault(aid, time.monotonic() + self.start_grace_s)
            if time.monotonic() < deadline:
                return
            census = self._contain(a)
            if census.verified_empty:
                self._unlaunched(a, "starting-no-receipt")
            else:
                self._quarantine(a, census, "start grace expired without a receipt")
            return
        if a["guardian_pid"] and procs.same_process(a["guardian_pid"], a["boot_id"], a["proc_start"]):
            if time.monotonic() >= self._census_next.get(aid, 0):
                self._record_owned(a)
                self._census_next[aid] = time.monotonic() + .5
            return  # Re-adopted solely by receipt identity, not parentage.
        census = self._contain(a)
        if not census.verified_empty:
            self._kill_attempt(a, lost=True)
        else:
            self._lost(a)

    def _contain(self, a: dict):
        return procs.containment(a.get("pgid"), a.get("guardian_pid"), a.get("child_pid"), a["attempt_id"], root=str(self.root))

    def _record_owned(self, a: dict) -> None:
        census = self._contain(a)
        if not procs.same_process(a["guardian_pid"], a["boot_id"], a["proc_start"]):
            return
        evidence = json.loads(a["evidence_json"] or "{}")
        before = dict(evidence.get("owned_identities", {}))
        owned = dict(before)
        owned.update({str(pid): dataclasses.asdict(ident) for pid, ident in census.identities.items() if pid in census.group_pids})
        if owned != before:
            evidence["owned_identities"] = owned
            with self.store.transaction("attempt.processes_recorded", job_id=a["job_id"], attempt_id=a["attempt_id"]) as tx:
                tx.execute("UPDATE attempts SET evidence_json=? WHERE attempt_id=?", (json.dumps(evidence), a["attempt_id"]))

    def _unlaunched(self, a: dict, detail: str) -> None:
        with self.store.transaction("attempt.no_launch", job_id=a["job_id"], attempt_id=a["attempt_id"], data={"detail": detail}) as tx:
            job = self._job(a["job_id"])
            cancel = bool(job["cancel_requested_at"])
            tx.execute("UPDATE attempts SET state=?,outcome_class='unknown',outcome_detail=?,finished_at=? WHERE attempt_id=?",
                       ("interrupted" if cancel else "failed", detail, utcnow(), a["attempt_id"]))
            tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (a["attempt_id"], job["job_id"]))
            retry = not cancel and a["seq"] < job["max_attempts"]
            state = "queued" if retry else "cancelled" if cancel else "failed"
            rc = None if retry else 130 if cancel else 1
            tx.execute("UPDATE jobs SET state=?,rc=?,finished_at=?,wait_reason=NULL,next_check_at=NULL WHERE job_id=?", (state, rc, None if retry else utcnow(), job["job_id"]))
            if not retry:
                self._notice(tx, job, "unknown", rc, None, detail)
        self._notify()

    def _begin_finalizing(self, a: dict, receipt: dict) -> None:
        with self.store.transaction("attempt.finalizing", job_id=a["job_id"], attempt_id=a["attempt_id"]) as tx:
            tx.execute("UPDATE attempts SET state='finalizing',rc=?,signal=?,child_pid=?,finished_at=? WHERE attempt_id=? AND state IN ('reserved','starting','running')",
                       (receipt["rc"], receipt.get("signal"), receipt.get("child_pid"), receipt.get("finished_at", utcnow()), a["attempt_id"]))
        self._boundary("finalizing", a["job_id"], a["attempt_id"])
        self._notify()

    def _kill_attempt(self, a: dict, *, lost: bool = False) -> None:
        census = self._contain(a)
        evidence = json.loads(a["evidence_json"] or "{}")
        # Only group members observed while the recorded leader is still ours
        # may become additional signal targets. Escaped/new marker pids remain
        # evidence for quarantine, never authority inferred from a PID alone.
        owned = {int(pid): procs.ProcessIdentity(**value) for pid, value in evidence.get("owned_identities", {}).items()}
        leader_live = a["guardian_pid"] and procs.same_process(a["guardian_pid"], a["boot_id"], a["proc_start"])
        if leader_live:
            owned.update({pid: ident for pid, ident in census.identities.items() if pid in census.group_pids})
        evidence["owned_identities"] = {str(pid): dataclasses.asdict(ident) for pid, ident in owned.items()}
        with self.store.transaction("attempt.kill_started", job_id=a["job_id"], attempt_id=a["attempt_id"]) as tx:
            tx.execute("UPDATE attempts SET killed_by=COALESCE(killed_by,?),evidence_json=? WHERE attempt_id=?", ("recovery" if lost else "operator", json.dumps(evidence), a["attempt_id"]))
        if a.get("pgid"):
            procs.signal_group(a["pgid"], signal.SIGTERM, boot_id=a["boot_id"], proc_start=a["proc_start"])
        deadline = time.monotonic() + self.term_grace_s
        while not census.verified_empty and time.monotonic() < deadline:
            if self.stopping.wait(min(.1, max(0, deadline - time.monotonic()))):
                return
            census = self._contain(a)
        escalated = not census.verified_empty
        if escalated and a.get("pgid"):
            procs.signal_group(a["pgid"], signal.SIGKILL, boot_id=a["boot_id"], proc_start=a["proc_start"])
        census = self._contain(a)
        for pid in census.live_pids:
            if pid in owned:
                procs.signal_process(owned[pid], signal.SIGKILL)
        # Give exited children a chance to be reaped; zombies are already absent
        # from the census. No SQLite transaction is open while waiting.
        self.stopping.wait(.05)
        census = self._contain(a)
        if not census.verified_empty:
            self._quarantine(a, census, "termination could not verify containment")
            return
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        receipt = self._read_json(adir / "exit.json")
        if lost and receipt is None:
            self._lost(a)
            return
        if receipt is None:
            sig = signal.SIGKILL if escalated else signal.SIGTERM
            receipt = {"rc": -int(sig), "signal": int(sig), "child_pid": a["child_pid"],
                       "wall_s": age(a["started_at"]), "finished_at": utcnow(), "killed_by": a.get("killed_by") or "operator"}
            self._publish("exit", adir / "exit.json", json_bytes(receipt))
        self._begin_finalizing(a, receipt)

    def _quarantine(self, a: dict, census, reason: str) -> None:
        detail = json.dumps({"reason": reason, **census.to_dict()}, sort_keys=True)
        with self.store.transaction("attempt.quarantined", job_id=a["job_id"], attempt_id=a["attempt_id"], data={"containment": census.to_dict()}) as tx:
            job = self._job(a["job_id"])
            tx.execute("UPDATE attempts SET state='quarantined',quarantine_reason=?,finished_at=? WHERE attempt_id=?", (detail, utcnow(), a["attempt_id"]))
            tx.execute("DELETE FROM leases WHERE holder=? AND lease_key LIKE 'lane:%'", (a["attempt_id"],))
            state, rc = ("cancelled", 130) if job["cancel_requested_at"] else ("lost", 125)
            tx.execute("UPDATE jobs SET state=?,rc=?,finished_at=? WHERE job_id=?", (state, rc, utcnow(), a["job_id"]))
            self._notice(tx, job, "unknown", a.get("rc"), None, "quarantined: " + detail)
        self._notify()

    def _resolve_quarantine(self, a: dict, args: protocol.KillArgs) -> None:
        census = self._contain(a)
        if not args.force_release and not census.verified_empty:
            with self.store.transaction("quarantine.still_live", job_id=a["job_id"], attempt_id=a["attempt_id"], data=census.to_dict()) as tx:
                tx.execute("UPDATE attempts SET quarantine_reason=? WHERE attempt_id=?", (json.dumps(census.to_dict()), a["attempt_id"]))
            return
        artifacts = []
        if census.verified_empty:
            artifacts, _ = self._salvage(self._job(a["job_id"]), a)
        with self.store.transaction("quarantine.force_release" if args.force_release else "quarantine.confirmed_dead", job_id=a["job_id"], attempt_id=a["attempt_id"], data={"operator_note": args.operator_note, "containment": census.to_dict(), "override": args.force_release}) as tx:
            for artifact in artifacts:
                self.store.add_artifact(a["attempt_id"], **artifact)
            tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (a["job_id"], a["attempt_id"]))
            tx.execute("UPDATE attempts SET state=? WHERE attempt_id=?", ("interrupted" if self._job(a["job_id"])["cancel_requested_at"] else "lost", a["attempt_id"]))
        self._notify()

    def _lost(self, a: dict) -> None:
        self._finalize(a, lost=True)

    def _saved_launch(self, a: dict) -> Launch:
        if a["attempt_id"] in self._launches:
            return self._launches[a["attempt_id"]]
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        value = self._read_json(adir / "launch.json")
        if value:
            value.update(env_add={"SUBFLEET_LANE": a["lane_id"]}, argv=tuple(value["argv"]), env_remove=tuple(value["env_remove"]))
            return Launch(**value)
        job = self._job(a["job_id"])
        return Launch((), {}, (), job.get("worktree") or job["workdir"], job["prompt_path"], str(adir / "stdout"), str(adir / "stderr"), None, None)

    def _salvage(self, job: dict, a: dict) -> tuple[list[dict], str | None]:
        if job["sandbox"] != "workspace-write":
            return [], None
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        receipt_path = adir / "salvage.json"
        receipt = self._read_json(receipt_path)
        if receipt is None:
            baseline = json.loads(a["evidence_json"] or "{}").get("baseline_commit") or job["workdir_head"]
            result = salvage(job.get("worktree") or job["workdir"], baseline, a["seq"],
                             writable=True, state="finalizing", timestamp=a["reserved_at"])
            receipt = {"result": dataclasses.asdict(result) if result else None,
                       "checkpoint": git_head(job.get("worktree") or job["workdir"])}
            self._publish("salvage", receipt_path, json_bytes(receipt))
            self._boundary("salvage", job["job_id"], a["attempt_id"])
        result = receipt["result"]
        if not result:
            return [], receipt["checkpoint"]
        ref = result.get("ref") or result.get("ref_name")
        commit = result.get("commit") or result.get("commit_sha")
        return [{"role": "salvage", "path": ref, "sha256": hashlib.sha256(commit.encode()).hexdigest(), "bytes": 0}], receipt["checkpoint"]

    @staticmethod
    def _artifact(path: Path, role: str) -> dict | None:
        try:
            contents = path.read_bytes()
        except FileNotFoundError:
            return None
        return {"role": role, "path": str(path), "sha256": hashlib.sha256(contents).hexdigest(), "bytes": len(contents)}

    @staticmethod
    def _restore_outcome(data: dict) -> Outcome:
        data = dict(data)
        data["cls"] = OutcomeClass(data["cls"])
        data["readings"] = tuple(Reading(**{**row, "label": ReadingLabel(row["label"])}) for row in data.get("readings", ()))
        if data.get("closure"):
            closure = data["closure"]
            data["closure"] = Closure(**{**closure, "reason": ClosureReason(closure["reason"]),
                                         "clock_source": ClockSource(closure["clock_source"])})
        return Outcome(**data)

    def _finalize(self, a: dict, *, lost: bool = False) -> None:
        job = self._job(a["job_id"])
        actual = self.store.get_attempt(a["attempt_id"])
        current = self.store.one("SELECT MAX(seq) n FROM attempts WHERE job_id=?", (a["job_id"],))["n"]
        if job["state"] in TERMINAL or job["accepted_attempt_id"] or current != a["seq"] or actual["state"] not in ("starting", "running", "finalizing"):
            return
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        adir.mkdir(mode=0o700, exist_ok=True)
        census = self._contain(a)
        if not census.verified_empty:
            # A receipt is written just before guardian exit; allow that small
            # interval without mistaking the guardian itself for an escape.
            if not census.unverifiable and census.live_pids <= {a.get("guardian_pid")}:
                return
            self._quarantine(a, census, "writers remain after exit receipt")
            return
        launch = self._saved_launch(a)
        # Both stream-json CLIs write their raw protocol to stdout. Freeze that
        # stream once after containment when no separate raw file was supplied.
        if launch.raw_stream_path and not Path(launch.raw_stream_path).exists():
            stdout = Path(launch.stdout_path)
            if stdout.is_file():
                self._publish("raw-stream", Path(launch.raw_stream_path), stdout.read_bytes())
        lane = self.store.get_lane(a["lane_id"])
        adapter = get_adapter(lane.provider)
        receipt = self._read_json(adir / "exit.json")
        if not receipt:
            lost = True
        rc = None if lost else receipt["rc"]
        if lost:
            outcome = Outcome(OutcomeClass.UNKNOWN, "guardian lost without exit receipt")
            attest_status, served_model = "unattested", None
        else:
            result_path = adir / "finalization.json"
            result = self._read_json(result_path)
            if result is None:
                exit_info = ExitInfo(**{k: receipt.get(k) for k in ("rc", "signal", "wall_s", "child_pid", "spawn_error")})
                outcome = adapter.classify(adir, launch, exit_info)
                if exit_info.spawn_error:
                    outcome = dataclasses.replace(outcome, detail=exit_info.spawn_error)
                attest = adapter.attest(adir, launch, outcome, a["model_requested"])
                result = {"outcome": dataclasses.asdict(outcome), "attestation": dataclasses.asdict(attest)}
                self._publish("finalization", result_path, json_bytes(result))
            outcome = self._restore_outcome(result["outcome"])
            attest_status = Attestation(result["attestation"]["status"]).value
            served_model = result["attestation"]["served_model"]
        deliverable_path = adir / "deliverable.md"
        if not deliverable_path.exists() and not lost:
            contents = adapter.deliverable(adir, launch, outcome)
            self._publish("deliverable", deliverable_path, contents or b"")
        deliverable = self._artifact(deliverable_path, "deliverable")
        if outcome.cls == OutcomeClass.OK and (not deliverable or not deliverable["bytes"]):
            outcome = dataclasses.replace(outcome, cls=OutcomeClass.UNKNOWN, detail="empty deliverable with rc 0")
        artifacts = [x for x in [deliverable,
                     self._artifact(Path(launch.stdout_path), "stdout"),
                     self._artifact(Path(launch.stderr_path), "stderr"),
                     self._artifact(adir / "launch.json", "launch"),
                     self._artifact(Path(launch.stdin_path), "prompt-sent")
                     if launch.stdin_path and Path(launch.stdin_path).name == "prompt.sent.md" else None,
                     self._artifact(adir / "lane.log", "lane-log"),
                     self._artifact(Path(launch.raw_stream_path), "raw-stream") if launch.raw_stream_path else None,
                     self._artifact(self.root / "jobs" / job["job_id"] / "manifest.json", "manifest")] if x]
        salvage_artifacts, checkpoint = self._salvage(job, a)
        artifacts.extend(salvage_artifacts)
        with self.store.transaction("attempt.accepted", job_id=a["job_id"], attempt_id=a["attempt_id"]) as tx:
            job = self._job(a["job_id"])
            current = tx.execute("SELECT MAX(seq) FROM attempts WHERE job_id=?", (job["job_id"],)).fetchone()[0]
            actual = self.store.get_attempt(a["attempt_id"])
            eligible_states = ("starting", "running", "finalizing") if lost else ("finalizing",)
            if current != a["seq"] or actual["state"] not in eligible_states or job["accepted_attempt_id"] or job["state"] in TERMINAL:
                return
            cancel = bool(job["cancel_requested_at"])
            ok = not lost and rc == 0 and outcome.cls == OutcomeClass.OK
            previous_transient = tx.execute("SELECT count(*) FROM attempts WHERE job_id=? AND lane_id=? AND outcome_class='transient' AND attempt_id!=?", (job["job_id"], a["lane_id"], a["attempt_id"])).fetchone()[0]
            retry = (not cancel and a["seq"] < job["max_attempts"] and
                     ((lost and job["sandbox"] == "read-only") or
                      (outcome.cls == OutcomeClass.LIMITED and not job["pinned_lane"]) or
                      (outcome.cls == OutcomeClass.TRANSIENT and not (job["pinned_lane"] and previous_transient))))
            attempt_state = "interrupted" if cancel else "lost" if lost else "succeeded" if ok else "failed"
            job_state = "cancelled" if cancel else "waiting" if retry else "lost" if lost else "succeeded" if ok else "failed"
            evidence = json.loads(a["evidence_json"] or "{}")
            evidence.update(classification=outcome.evidence, checkpoint=checkpoint)
            tx.execute("UPDATE attempts SET state=?,rc=?,outcome_class=?,outcome_detail=?,evidence_json=?,attestation=?,model_served=?,native_session_id=COALESCE(?,native_session_id),transcript_path=?,finished_at=COALESCE(finished_at,?) WHERE attempt_id=?",
                       (attempt_state, rc, outcome.cls.value, outcome.detail, json.dumps(evidence), attest_status, served_model,
                        outcome.native_session_id, outcome.transcript_path, utcnow(), a["attempt_id"]))
            for artifact in artifacts:
                self.store.add_artifact(a["attempt_id"], **artifact)
            if outcome.cls == OutcomeClass.AUTH_DEAD:
                self.store.update_lane(a["lane_id"], enabled=0)
                self.timers.record_auth_dead(a["lane_id"])
            for reading in outcome.readings:
                self.store.add_reading(reading)
            if outcome.closure:
                self.store.add_closure(outcome.closure)
            tx.execute("DELETE FROM leases WHERE holder=? AND lease_key LIKE 'lane:%'", (a["attempt_id"],))
            job_rc = 130 if cancel else None if retry else 125 if lost else rc
            if not cancel and not retry and not lost:
                job_rc = {OutcomeClass.LIMITED: 4,  # C-17.3: a final limited attempt reports 4, pinned or not
                          OutcomeClass.AUTH_DEAD: 5, OutcomeClass.CLI_TOO_OLD: 6}.get(outcome.cls, rc)
            accepted = a["attempt_id"] if ok and not cancel else None
            next_check = after(60 if outcome.cls == OutcomeClass.TRANSIENT else 0) if retry else None
            tx.execute("UPDATE jobs SET state=?,rc=?,accepted_attempt_id=?,finished_at=?,wait_reason=?,next_check_at=? WHERE job_id=?",
                       (job_state, job_rc, accepted, None if retry else utcnow(), "capacity" if retry else None, next_check, job["job_id"]))
            if not retry:
                if not accepted:
                    tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (job["job_id"], a["attempt_id"]))
                uncertainty = f"; {attest_status}" if attest_status != "attested" else ""
                self._notice(tx, job, outcome.cls.value, rc, str(deliverable_path) if deliverable else None, outcome.detail + uncertainty)
        if not retry:
            self._boundary("terminal", a["job_id"], a["attempt_id"])
            self._boundary("notice", a["job_id"], a["attempt_id"])
        if accepted:
            self._export(job["job_id"])
        self._notify()

    def _export(self, job_id: str) -> None:
        with self._busy_lock:
            lock = self._export_locks.setdefault(job_id, threading.Lock())
        with lock:
            self._export_locked(job_id)

    def _export_locked(self, job_id: str) -> None:
        job = self._job(job_id)
        if not job["accepted_attempt_id"]:
            return
        if job["out_path"]:
            lease = self.store.one("SELECT holder FROM leases WHERE lease_key=?", (f"out:{job['out_path']}",))
            if not lease or lease["holder"] != job_id:
                return  # A replay cannot overwrite a newer owner's output.
        a = self.store.get_attempt(job["accepted_attempt_id"])
        artifact = self.store.one("SELECT * FROM artifacts WHERE attempt_id=? AND role='deliverable'", (a["attempt_id"],))
        export_error = None
        exported = None
        if job["out_path"] and not self.store.one("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'", (a["attempt_id"],)):
            try:
                contents = Path(artifact["path"]).read_bytes()
                destination = Path(job["out_path"])
                if hashlib.sha256(contents).hexdigest() != artifact["sha256"]:
                    raise OSError("accepted deliverable digest changed")
                self._publish("export", destination, contents)
                self._boundary("export", job_id, a["attempt_id"])
                exported = {"role": "export", "path": str(destination), "sha256": artifact["sha256"], "bytes": artifact["bytes"]}
            except OSError as exc:
                export_error = f"export failed: {type(exc).__name__} (errno={exc.errno})"
        with self.store.transaction("job.export_failed" if export_error else "job.exported", job_id=job_id, attempt_id=a["attempt_id"]) as tx:
            if exported:
                self.store.add_artifact(a["attempt_id"], **exported)
            if export_error:
                tx.execute("UPDATE jobs SET export_error=? WHERE job_id=?", (export_error, job_id))
                tx.execute("UPDATE notices SET text=text || ? WHERE job_id=?", ("\n" + export_error, job_id))
            tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (job_id, a["attempt_id"]))
        self._notify()

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
        self.timers.cancel.set()
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
        self.timers.stop()
        for pool in (self.readers, self.requests, self.waiters, self.workers):
            pool.shutdown(wait=True, cancel_futures=True)
        self.store.close()
        (self.root / "daemon.sock").unlink(missing_ok=True)
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        self._lock_finalizer()
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
