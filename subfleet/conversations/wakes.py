"""Durable, bounded conversation check-backs (C-24.10).

The control loop owns polling; requests never invoke gh. Acceptance, consumption,
and the unattended counter share the message transaction, including after restart.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import shlex
import subprocess
import time
import uuid
from datetime import UTC, datetime

from .store import ConversationError, canonical_native, utcnow
from .turn import TERMINAL_STATES

MAX_STREAK = 8
COOLDOWN_S = 30 * 60
PR_INTERVAL_S = 60
MIN_TIMER_S = 300
MAX_TARGETS = 16
EVALUATION_INTERVAL_S = 1.0
PR = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)\Z")
SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_requests (
 conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, kind TEXT NOT NULL,
 payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 observed_json TEXT, ready_json TEXT, message_id TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(conversation_id,request_id,kind)
);
CREATE UNIQUE INDEX IF NOT EXISTS wake_pending_kind ON wake_requests(conversation_id,kind) WHERE state='pending';
CREATE TABLE IF NOT EXISTS wake_runs (
 conversation_id TEXT NOT NULL, job_id TEXT NOT NULL, message_id TEXT NOT NULL,
 PRIMARY KEY(conversation_id,job_id)
);
CREATE TABLE IF NOT EXISTS wake_notice_repairs (
 job_id TEXT PRIMARY KEY, delivered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wake_historical_runs (job_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS wake_meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS wake_pr_refusals (
 conversation_id TEXT NOT NULL, target TEXT NOT NULL, error TEXT NOT NULL,
 PRIMARY KEY(conversation_id,target)
);
CREATE TABLE IF NOT EXISTS wake_pr_windows (
 conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, target TEXT NOT NULL,
 event_since TEXT NOT NULL, PRIMARY KEY(conversation_id,request_id,target)
);
"""


def normalize(*, runs=None, prs=None, at=None, note="", now=None) -> dict:
    now = time.time() if now is None else now
    if not isinstance(note, str) or len(note) > 4000:
        raise ConversationError("bad-wake", "note must be a string of at most 4000 characters")
    result = {}
    for kind, targets in (("runs", runs), ("pr", prs)):
        if targets:
            if not isinstance(targets, list) or not all(isinstance(x, str) and x for x in targets):
                raise ConversationError("bad-wake", "targets must be lists of nonempty strings")
            targets = list(dict.fromkeys(targets))
            if len(targets) > MAX_TARGETS or any(len(x) > 200 for x in targets):
                raise ConversationError("bad-wake", "at most 16 targets per kind, each at most 200 characters")
            if kind == "pr" and any(not PR.fullmatch(x) for x in targets):
                raise ConversationError("bad-wake", "PRs use OWNER/REPO#N")
            result[kind] = {"targets": targets, "note": note}
    if at is not None:
        try:
            instant = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if instant.tzinfo is None or instant.timestamp() < now + MIN_TIMER_S:
                raise ValueError()
        except (ValueError, AttributeError, OverflowError):
            raise ConversationError("bad-wake", "at needs an ISO timestamp with a timezone, at least 5 minutes away")
        result["time"] = {"at": instant.timestamp(), "note": note}
    if not result:
        raise ConversationError("bad-wake", "name runs, PRs, or a time")
    return result


def _wake_line(line: str) -> str | None:
    if len(line) - len(line.lstrip()) >= 4:
        return None
    line = re.sub(r"^ {0,3}(?:[-*] )?", "", line)
    if line.startswith("**WAKE-ME:"):
        line = line[2:]
        if line.endswith("**"):
            line = line[:-2]
        line = line.replace("WAKE-ME:**", "WAKE-ME:", 1)
    return line if line.startswith("WAKE-ME:") else None


def _trailing_wake_lines(text: str, *, with_positions: bool = False) -> list:
    lines = text.rstrip().splitlines()
    top_level = []
    fence = None
    for line in lines:
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        top_level.append(fence is None and marker is None)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
    if lines and top_level[-1] and re.fullmatch(r"(?:DONE|WAITING ON MAX(?: .*?)?|HANDED TO .+)", lines[-1]):
        lines.pop()
        top_level.pop()
    result = []
    for position, (line, allowed) in reversed(list(enumerate(zip(lines, top_level)))):
        if not line.strip():
            continue
        normalized = _wake_line(line) if allowed else None
        if normalized is None:
            break
        result.append((position, normalized) if with_positions else normalized)
    return list(reversed(result))


def trailing_requests(text: str) -> list[dict]:
    """Top-level trailing requests, with an optional standard close-out line."""
    requests = []
    for line in _trailing_wake_lines(text):
        fields = {}
        for token in shlex.split(line[len("WAKE-ME:"):]):
            key, sep, value = token.partition("=")
            if not sep or key not in ("runs", "prs", "at", "note") or key in fields:
                raise ConversationError("bad-wake", "invalid WAKE-ME field")
            fields[key] = value
        if not any(fields.get(k) for k in ("runs", "prs", "at")):
            raise ConversationError("bad-wake", "name runs, PRs, or a time")
        requests.append({"runs": fields["runs"].split(",") if fields.get("runs") else None,
                         "prs": fields["prs"].split(",") if fields.get("prs") else None,
                         "at": fields.get("at"), "note": fields.get("note", "")})
    return requests


def eligible(tx, cid: str, now: float) -> bool:
    c = tx.execute("SELECT * FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
    if not c or c["blocked_by"] or c["legacy_hold"] or c["archived_at"]:
        return False
    if c["wake_streak"] >= MAX_STREAK and now - (c["last_wake_at"] or 0) < COOLDOWN_S:
        return False
    terminal = ",".join("?" for _ in TERMINAL_STATES)
    return not tx.execute(f"SELECT 1 FROM messages WHERE conversation_id=? AND state NOT IN ({terminal}) LIMIT 1",
                          (cid, *TERMINAL_STATES)).fetchone()


def claim(tx, cid: str, mid: str, data: dict, *, accepted_at: str) -> None:
    """Called in submit_message's transaction. A racing person/block wins."""
    if not eligible(tx, cid, data["now"]):
        raise ConversationError("wake-deferred", "conversation is busy, held, or throttled")
    for kind, request_id in data["requests"]:
        if not tx.execute("SELECT 1 FROM wake_requests WHERE conversation_id=? AND kind=? AND request_id=? "
                          "AND state='pending'", (cid, kind, request_id)).fetchone():
            raise ConversationError("wake-deferred", "request was replaced or already fired")
    for job_id in data["runs"]:
        if tx.execute("SELECT 1 FROM wake_runs WHERE conversation_id=? AND job_id=?", (cid, job_id)).fetchone():
            raise ConversationError("wake-deferred", "completion already delivered")
    announced = set(data["requests"])
    for _, request_id in data["requests"]:
        # The PR worker may have recorded an event on another kind of this request
        # after the tick read it. Consuming that kind unannounced would make the
        # event a baseline; the next evaluation includes it instead.
        for row in tx.execute("SELECT kind FROM wake_requests WHERE conversation_id=? AND request_id=? "
                              "AND state='pending' AND ready_json IS NOT NULL", (cid, request_id)):
            if (row["kind"], request_id) not in announced:
                raise ConversationError("wake-deferred", "another kind of this request became ready")
    for kind, request_id in data["requests"]:
        # Kinds in one request are alternatives: the first ready trigger consumes
        # all of them. Independent requests ready this cycle share one message.
        tx.execute("UPDATE wake_requests SET state='fired',message_id=? WHERE conversation_id=? AND request_id=? AND state='pending'",
                   (mid, cid, request_id))
    tx.executemany("INSERT INTO wake_runs VALUES(?,?,?)", [(cid, job_id, mid) for job_id in data["runs"]])
    tx.executemany("INSERT OR IGNORE INTO wake_notice_repairs VALUES(?,?)",
                   [(job_id, accepted_at) for job_id in data["runs"]])
    tx.execute("UPDATE conversations SET wake_streak=wake_streak+1,last_wake_at=? WHERE conversation_id=?", (data["now"], cid))


class WakeEngine:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.now = time.time
        self.poller = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="subfleet-pr-wakes")
        self._poll_future = None
        self._next_poll = 0.0
        self._next_completions = 0.0
        self._next_requests = 0.0
        self._started = False
        # Catalog-only services can be constructed before a job store exists.
        # Start immediately when possible to snapshot historical completions
        # before any new work; otherwise resolve the store on the first tick.
        if getattr(service.daemon, "store", None) is not None:
            self.start()

    def start(self) -> None:
        if self._started:
            return
        activation = self.now()
        historical = self.service.daemon.store.query(
            "SELECT job_id FROM jobs WHERE kind<>'turn' AND state IN "
            "('succeeded','failed','cancelled','lost','quarantined')") if not self.store.one(
                "SELECT 1 FROM wake_meta WHERE key='automatic-since'") else []
        with self.store.transaction() as tx:
            if tx.execute("INSERT OR IGNORE INTO wake_meta VALUES('automatic-since',?)", (activation,)).rowcount:
                tx.executemany("INSERT OR IGNORE INTO wake_historical_runs VALUES(?)",
                               [(row["job_id"],) for row in historical])
            if not tx.execute("SELECT 1 FROM wake_meta WHERE key='notice-queue-v1'").fetchone():
                # One upgrade repair, not a repeated scan of historical wake ids.
                tx.execute("INSERT OR IGNORE INTO wake_notice_repairs SELECT w.job_id,m.created_at "
                           "FROM wake_runs w JOIN messages m USING(message_id)")
                tx.execute("INSERT INTO wake_meta VALUES('notice-queue-v1',?)", (self.now(),))
        self._started = True

    def close(self) -> None:
        # gh has a hard timeout. The worker must end before its stores close.
        self.poller.shutdown(wait=True, cancel_futures=True)

    def control_tick(self) -> None:
        """Network polling never holds the dispatch/control-loop worker."""
        if self._poll_future is not None and self._poll_future.done():
            try:
                self._poll_future.result()
            except Exception as exc:
                self.service.log.warning("PR wake worker failed: %s", exc)
            self._poll_future = None
        if self._poll_future is None and self.now() >= self._next_poll:
            now = self.now()
            last = self.store.one("SELECT value FROM wake_meta WHERE key='pr-polled'")
            # The next check is due when the once-a-minute mark allows, not a full
            # interval after a check the mark refused.
            due = last["value"] + PR_INTERVAL_S if last else now
            pending = self.store.query("SELECT * FROM wake_requests WHERE state='pending' AND kind='pr' AND ready_json IS NULL")
            if pending and now >= due:
                self._next_poll = now + PR_INTERVAL_S
                self._poll_future = self.poller.submit(self._poll_prs, pending)
            else:
                self._next_poll = due if pending and due > now else now + PR_INTERVAL_S
        now = self.now()
        scan_completions = now >= self._next_completions
        if scan_completions:
            self._next_completions = now + EVALUATION_INTERVAL_S
        scan_requests = scan_completions or now >= self._next_requests
        if scan_requests:
            self._next_requests = now + EVALUATION_INTERVAL_S
        self.tick(poll=False, scan_completions=scan_completions, scan_requests=scan_requests)

    def register(self, cid: str, request_id: str, spec: dict, *, event_since: float | None = None) -> dict:
        conversation = self.store.conversation(cid)
        for job_id in spec.get("runs", {}).get("targets", []):
            if self.store.one("SELECT 1 FROM wake_requests WHERE conversation_id=? AND request_id=? AND kind='runs'",
                              (cid, request_id)):
                break  # a completed request remains retryable after job retention
            job = self.service.daemon.store.one("SELECT caller_session,parent_job_id FROM jobs WHERE job_id=? AND kind<>'turn'", (job_id,))
            parent = self.service.daemon.store.one("SELECT name FROM jobs WHERE job_id=? AND kind='turn'",
                                                  (job["parent_job_id"],)) if job else None
            owned = parent and parent["name"] == f"turn-{cid}"
            if not job or not (owned or (conversation["native_session_id"] and job["caller_session"] and
                    canonical_native(job["caller_session"]) == canonical_native(conversation["native_session_id"]))):
                raise ConversationError("bad-wake", f"run {job_id} is not this conversation's")
        with self.store.transaction() as tx:
            existing = {r["kind"]: json.loads(r["payload_json"]) for r in tx.execute(
                "SELECT kind,payload_json FROM wake_requests WHERE conversation_id=? AND request_id=?", (cid, request_id))}
            if existing:
                if existing != spec:
                    raise ConversationError("wake-id-conflict", "request id already has another payload")
                return {"request_id": request_id, "kinds": list(spec)}
            # Baselines are per target: the last fired observation of each, then the
            # watch this request supersedes. That watch's undelivered events stay
            # ready rather than becoming a baseline (an observation before them does).
            baseline, carried = {}, set()
            for row in tx.execute("SELECT observed_json FROM wake_requests WHERE conversation_id=? AND kind='pr' "
                                  "AND state='fired' AND observed_json IS NOT NULL ORDER BY rowid", (cid,)):
                baseline.update(json.loads(row["observed_json"]))
            superseded = tx.execute("SELECT request_id,payload_json,created_at,observed_json,ready_json "
                                    "FROM wake_requests WHERE conversation_id=? "
                                    "AND kind='pr' AND state='pending'", (cid,)).fetchone()
            # Retained targets keep their unobserved event window even if a poll
            # has not run or is still in flight. New targets start at this request.
            # Pre-upgrade watches have no window rows: their creation time is the floor.
            windows = {}
            if superseded:
                windows = {p: superseded["created_at"] for p in json.loads(superseded["payload_json"])["targets"]}
                windows.update({row["target"]: row["event_since"] for row in tx.execute(
                    "SELECT target,event_since FROM wake_pr_windows WHERE conversation_id=? AND request_id=?",
                    (cid, superseded["request_id"]))})
            if superseded and superseded["observed_json"]:
                ready = set(json.loads(superseded["ready_json"] or "[]"))
                for target, snapshot in json.loads(superseded["observed_json"]).items():
                    if target in ready and not snapshot.get("error"):
                        carried.add(target)
                    elif target not in ready:
                        baseline[target] = snapshot
            for kind, payload in spec.items():
                encoded = json.dumps(payload, sort_keys=True)
                previous = tx.execute("SELECT payload_json FROM wake_requests WHERE conversation_id=? AND request_id=? AND kind=?",
                                      (cid, request_id, kind)).fetchone()
                if previous:
                    if previous[0] != encoded:
                        raise ConversationError("wake-id-conflict", "request id already has another payload")
                    continue
                tx.execute("UPDATE wake_requests SET state='superseded' WHERE conversation_id=? AND kind=? AND state='pending'", (cid, kind))
                observed = {p: baseline[p] for p in payload["targets"] if p in baseline} if kind == "pr" else None
                ready = [p for p in payload["targets"] if p in carried] if kind == "pr" else []
                if ready:
                    old = json.loads(superseded["observed_json"])
                    observed = {**(observed or {}), **{p: old[p] for p in ready}}
                threshold = self.now() if event_since is None else event_since
                created_at = datetime.fromtimestamp(threshold, UTC).isoformat()
                tx.execute("INSERT INTO wake_requests(conversation_id,request_id,kind,payload_json,created_at,observed_json,ready_json) "
                           "VALUES(?,?,?,?,?,?,?)",
                           (cid, request_id, kind, encoded, created_at,
                            json.dumps(observed) if observed else None, json.dumps(ready) if ready else None))
                if kind == "pr":
                    tx.executemany("INSERT INTO wake_pr_windows VALUES(?,?,?,?)",
                                   [(cid, request_id, p, windows.get(p, created_at)) for p in payload["targets"]])
        if "pr" in spec:
            # A new watch is checked on the next tick rather than up to a minute
            # later; the `pr-polled` mark still holds gh to one query a minute.
            self._next_poll = min(self._next_poll, self.now())
        return {"request_id": request_id, "kinds": list(spec)}

    def from_final(self, cid: str, mid: str, text: str) -> None:
        message = self.store.one("SELECT created_at,job_id FROM messages WHERE message_id=?", (mid,))
        started = self.service.daemon.store.one("SELECT created_at FROM jobs WHERE job_id=?", (message["job_id"],)) if message and message["job_id"] else None
        validation_time = datetime.fromisoformat((started or message)["created_at"].replace("Z", "+00:00")).timestamp() if message else self.now()
        raw_lines = text.rstrip().splitlines()
        start = len(raw_lines)
        while start and raw_lines[start - 1].startswith("WAKE-ME:"):
            start -= 1
        legacy_ids = {i: f"final:{mid}:{i - start}" for i in range(start, len(raw_lines))}
        entries = _trailing_wake_lines(text, with_positions=True)
        duplicates, new_ids = {}, {}
        for position, line in reversed(entries):
            digest = hashlib.sha256(line.encode()).hexdigest()
            ordinal = duplicates.get(digest, 0)
            duplicates[digest] = ordinal + 1
            new_ids[position] = f"final:{mid}:line:{digest}:{ordinal}"
        for index, (position, line) in enumerate(entries):
            # Preserve the legacy literal-tail ids. Newly recognised forms cannot
            # shift them and replay an already-fired timer after an upgrade.
            request_id = legacy_ids.get(position, new_ids[position])
            if self.store.one("SELECT 1 FROM wake_requests WHERE conversation_id=? AND request_id=?", (cid, request_id)):
                continue
            try:
                args = trailing_requests(line)[0]
                # The floor is measured from the turn's start: the agent wrote the
                # time during the turn, and settlement can follow by minutes (D-15).
                # A time already past at settlement is still refused.
                spec = normalize(**args, now=min(self.now(), validation_time))
                if "time" in spec and spec["time"]["at"] <= self.now():
                    raise ConversationError("bad-wake", "at is already past at settlement")
                self.register(cid, request_id, spec, event_since=validation_time)
            except (ConversationError, ValueError) as exc:
                self.service.log.warning("wake request in final text of %s refused: %s", mid, exc)
                if message:
                    self.store.append_events(conversation_id=cid, message_id=mid, attempt_id=f"wake:{mid}",
                        events=[("command", f"wake-refused:{request_id}", 0, "status",
                                 {"phase": "wake-refused", "detail": f"Wake request refused: {exc}"})],
                        stdout_offset=0, stdin_seq=0)

    def _completions(self) -> dict[str, list[dict]]:
        since = self.store.one("SELECT value FROM wake_meta WHERE key='automatic-since'")["value"]
        # Jobs use second-resolution timestamps. The initial snapshot excludes
        # already-completed jobs; the rounded floor retains new same-second work.
        cutoff = datetime.fromtimestamp(since, UTC).replace(microsecond=0).isoformat()
        conversations = self.store.query("SELECT * FROM conversations")
        by_id = {c["conversation_id"]: c for c in conversations}
        by_session = {canonical_native(c["native_session_id"]): c for c in conversations if c["native_session_id"]}
        result = {}
        # The job's caller is the same session resolved by runs --mine and notices.
        for job in self.service.daemon.store.query(
                "SELECT j.job_id,j.caller_session,j.state,j.out_path,j.accepted_attempt_id,j.created_at,p.name parent_name "
                "FROM jobs j LEFT JOIN jobs p ON j.parent_job_id=p.job_id AND p.kind='turn' WHERE j.kind<>'turn' "
                "AND julianday(COALESCE(j.finished_at,j.created_at))>=julianday(?) AND j.state IN ('succeeded','failed','cancelled','lost','quarantined') "
                "AND NOT EXISTS (SELECT 1 FROM notices n WHERE n.job_id=j.job_id "
                                                  "AND n.state IN ('surfaced','acknowledged') AND COALESCE(n.transport,'')<>'conversation')", (cutoff,)):
            if self.store.one("SELECT 1 FROM wake_historical_runs WHERE job_id=?", (job["job_id"],)):
                continue
            c = by_id.get((job["parent_name"] or "")[5:]) or by_session.get(canonical_native(job["caller_session"]))
            if not c or job["created_at"] < c["created_at"]:
                continue
            if not job["parent_name"] and not self.service.daemon.store.one(
                    "SELECT 1 FROM jobs WHERE kind='turn' AND name=? AND created_at<=? "
                    "AND (finished_at IS NULL OR finished_at>=?)", (f"turn-{c['conversation_id']}", job["created_at"], job["created_at"])):
                continue
            cid = c["conversation_id"]
            if not self.store.one("SELECT 1 FROM wake_runs WHERE conversation_id=? AND job_id=?", (cid, job["job_id"])):
                result.setdefault(cid, []).append(job)
        return result

    def _poll_prs(self, pending: list[dict]) -> None:
        requests = [r for r in pending if r["kind"] == "pr" and not r["ready_json"]]
        if not requests:
            return
        now = self.now()
        with self.store.transaction() as tx:
            last = tx.execute("SELECT value FROM wake_meta WHERE key='pr-polled'").fetchone()
            if last and now - last["value"] < PR_INTERVAL_S:
                return
            tx.execute("INSERT OR REPLACE INTO wake_meta VALUES('pr-polled',?)", (now,))
        targets = sorted({p for r in requests for p in json.loads(r["payload_json"])["targets"]})
        try:
            snapshots = query_prs(targets)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.service.log.warning("batched PR wake poll failed: %s", exc)
            return
        for r in requests:
            before = json.loads(r["observed_json"] or "{}")
            watched = json.loads(r["payload_json"])["targets"]
            windows = {row["target"]: row["event_since"] for row in self.store.query(
                "SELECT target,event_since FROM wake_pr_windows WHERE conversation_id=? AND request_id=?",
                (r["conversation_id"], r["request_id"]))}
            # A target already refused here is announced once; it stays watched so
            # that it can resolve later (a PR opened after the watch, access restored).
            refused = {row["target"] for row in self.store.query(
                "SELECT target FROM wake_pr_refusals WHERE conversation_id=?", (r["conversation_id"],))}
            changed = [p for p in watched if p in snapshots and (
                (snapshots[p].get("error") and p not in refused) or pr_changed(before.get(p), snapshots[p]) or
                ((p not in before or before[p].get("error")) and not snapshots[p].get("error") and
                 pr_event_since(snapshots[p], windows.get(p, r["created_at"]))))]
            observed = {**before, **{p: snapshots[p] for p in watched if p in snapshots}}
            with self.store.transaction() as tx:
                tx.execute("UPDATE wake_requests SET observed_json=?,ready_json=? WHERE conversation_id=? AND request_id=? "
                           "AND kind='pr' AND state='pending'", (json.dumps(observed), json.dumps(changed) if changed else None,
                                                               r["conversation_id"], r["request_id"]))
                tx.executemany("INSERT OR REPLACE INTO wake_pr_refusals VALUES(?,?,?)",
                               [(r["conversation_id"], p, snapshots[p]["error"]) for p in watched
                                if p in snapshots and snapshots[p].get("error")])
                tx.executemany("DELETE FROM wake_pr_refusals WHERE conversation_id=? AND target=?",
                               [(r["conversation_id"], p) for p in watched if p in snapshots and not snapshots[p].get("error")])

    def tick(self, *, poll: bool = True, scan_completions: bool = True, scan_requests: bool = True) -> None:
        self.start()
        self._surface_notices()
        if not scan_requests and not scan_completions:
            return
        pending = self.store.query("SELECT * FROM wake_requests WHERE state='pending'") if scan_requests else []
        if poll:
            self._poll_prs(pending)
            pending = self.store.query("SELECT * FROM wake_requests WHERE state='pending'")
        completions = self._completions() if scan_completions else {}
        covered = {r["conversation_id"]: set() for r in pending if r["kind"] == "runs"}
        for r in pending:
            if r["kind"] == "runs":
                covered[r["conversation_id"]].update(json.loads(r["payload_json"])["targets"])
        grouped = {}
        for r in pending:
            grouped.setdefault(r["conversation_id"], []).append(r)
        now = self.now()
        with self.store.read() as db:
            candidates = [cid for cid in grouped.keys() | completions.keys() if eligible(db, cid, now)]
        if not candidates:
            return
        # Read each target set in bounded batches, not one round trip per run
        # and per conversation. Claim rechecks conversation guards on write.
        job_store = self.service.daemon.store
        active_turns = {r["name"] for r in _target_rows(job_store,
            "SELECT name FROM jobs WHERE kind='turn' AND state NOT IN ('succeeded','failed','cancelled','lost') AND name IN ({})",
            [f"turn-{cid}" for cid in candidates])}
        leases = {r["lease_key"] for r in _target_rows(job_store,
            "SELECT lease_key FROM leases WHERE lease_key IN ({})", [f"conversation:{cid}" for cid in candidates])}
        targets = sorted({j for cid in candidates for j in covered.get(cid, set())})
        jobs_by_id = {j["job_id"]: j for j in _target_rows(job_store,
            "SELECT job_id,state,out_path,accepted_attempt_id FROM jobs WHERE job_id IN ({})", targets)}
        delivered = {(r["conversation_id"], r["job_id"]) for r in _target_rows(self.store,
            "SELECT conversation_id,job_id FROM wake_runs WHERE job_id IN ({})", targets)}
        noticed = {r["job_id"] for r in _target_rows(job_store,
            "SELECT job_id FROM notices WHERE state IN ('acknowledged','surfaced') "
            "AND COALESCE(transport,'')<>'conversation' AND job_id IN ({})", targets)}
        for cid in candidates:
            # Job leases and finalization may lag the message's terminal receipt.
            if f"turn-{cid}" in active_turns or f"conversation:{cid}" in leases:
                continue
            runs = {j["job_id"]: j for j in completions.get(cid, []) if j["job_id"] not in covered.get(cid, set())}
            ready, notes = [], []
            for r in sorted(grouped.get(cid, []), key=lambda r: r["kind"] != "runs"):
                payload = json.loads(r["payload_json"])
                kind = r["kind"]
                if kind == "runs":
                    jobs = [jobs_by_id.get(j) or
                            {"job_id": j, "state": "pruned"} for j in payload["targets"]]
                    if not all(j["state"] in ('succeeded','failed','cancelled','lost','quarantined','pruned') for j in jobs):
                        continue
                    undelivered = {j["job_id"]: j for j in jobs
                                   if (cid, j["job_id"]) not in delivered and j["job_id"] not in noticed}
                    if not undelivered:
                        # Only these runs already have their answer. Alternative
                        # timer and PR triggers still need delivery or resolution.
                        with self.store.transaction() as tx:
                            tx.execute("UPDATE wake_requests SET state='satisfied' WHERE conversation_id=? "
                                       "AND request_id=? AND kind='runs' AND state='pending'", (cid, r["request_id"]))
                        continue
                    runs.update(undelivered)
                elif kind == "time":
                    if now < payload["at"]:
                        continue
                    notes.append("Scheduled check-back is due.")
                elif not r["ready_json"]:
                    continue
                else:
                    observed = json.loads(r["observed_json"] or "{}")
                    changed = []
                    for target in json.loads(r["ready_json"]):
                        error = observed.get(target, {}).get("error")
                        if error:
                            notes.append(f"PR watch refused: {target} ({error}). Correct the reference and re-arm.")
                        else:
                            changed.append(target)
                    if changed:
                        notes.append("PR state changed: " + ", ".join(changed))
                ready.append((kind, r["request_id"]))
                if payload["note"]:
                    notes.append(payload["note"])
            if not runs and not ready:
                continue
            lines = []
            for job in runs.values():
                accepted = job.get("accepted_attempt_id") if job["state"] == "succeeded" else None
                deliverable = (str(self.service.root / "jobs" / accepted / "deliverable.md") if accepted else
                               job.get("out_path") if job["state"] == "succeeded" else None)
                lines.append(f"{job['job_id']} finished: {job['state']}; deliverable {deliverable or '(none)'}")
            text = "[Subfleet]\n" + "\n".join(lines + notes)
            try:
                c = self.store.conversation(cid)
                self.store.submit_message(conversation_id=cid, message_id=str(uuid.uuid4()), after_message_id=None,
                    text=text, attachments=[], settings=c["settings"], origin="wake",
                    wake_claim={"now": now, "requests": ready, "runs": list(runs)})
            except ConversationError as exc:
                if exc.reason != "wake-deferred":
                    raise
            else:
                self._surface_notices()
                self.service.daemon._notify()

    def _surface_notices(self) -> None:
        """Repair only outstanding deliveries, in bounded batches across stores."""
        rows = self.store.query("SELECT job_id,delivered_at FROM wake_notice_repairs LIMIT 500")
        if not rows:
            return
        with self.service.daemon.store.transaction("conversation.wake-notices") as tx:
            tx.executemany("UPDATE notices SET state='surfaced',transport='conversation',offered_at=? "
                           "WHERE job_id=? AND state='pending' AND julianday(created_at)<=julianday(?)",
                           [(utcnow(), r["job_id"], r["delivered_at"]) for r in rows])
        # If this deletion crashes, the next tick repeats an idempotent repair.
        with self.store.transaction() as tx:
            tx.executemany("DELETE FROM wake_notice_repairs WHERE job_id=? AND delivered_at=?",
                           [(r["job_id"], r["delivered_at"]) for r in rows])


def _target_rows(store, sql: str, targets: list[str]) -> list[dict]:
    rows = []
    for offset in range(0, len(targets), 500):
        batch = targets[offset:offset + 500]
        rows.extend(store.query(sql.format(",".join("?" for _ in batch)), tuple(batch)))
    return rows


def pr_changed(before: dict | None, after: dict) -> bool:
    if before is None or before.get("error"):
        return False  # first observation establishes a baseline, never an event
    if before.get("state") != after.get("state") and after.get("state") in ("MERGED", "CLOSED"):
        return True
    reviews = after.get("reviews", [])
    if any(r not in before.get("reviews", []) for r in reviews):
        return True
    checks = after.get("checks", [])
    return bool(checks and checks != before.get("checks") and all(c[0] in ("COMPLETED", "SUCCESS", "FAILURE", "ERROR") for c in checks))


def pr_event_since(snapshot: dict, created_at: str) -> bool:
    """Dated events in an unobserved window count, including after a refusal."""
    threshold = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    stamps = list(snapshot.get("review_times", {}).values())
    checks = snapshot.get("checks", [])
    if checks and all(c[0] in ("COMPLETED", "SUCCESS", "FAILURE", "ERROR") for c in checks):
        stamps.extend(c[3] for c in checks if len(c) > 3)
    if snapshot.get("state") == "MERGED":
        stamps.append(snapshot.get("merged_at"))
    elif snapshot.get("state") == "CLOSED":
        stamps.append(snapshot.get("closed_at"))
    for stamp in stamps:
        try:
            if stamp and datetime.fromisoformat(stamp.replace("Z", "+00:00")) > threshold:
                return True
        except (ValueError, TypeError, AttributeError):
            continue
    return False


def query_prs(targets: list[str]) -> dict:
    fields = """state mergedAt closedAt headRefOid commits(last:1) { nodes { commit { statusCheckRollup { contexts(first:100) { nodes {
        __typename ... on CheckRun { name status conclusion completedAt }
        ... on StatusContext { context state createdAt }
    } pageInfo { hasNextPage } } } } } }
    reviews(last:100) { nodes { id submittedAt state } }"""
    parts = []
    for index, target in enumerate(targets):
        owner, repo, number = PR.fullmatch(target).groups()
        parts.append(f'p{index}: repository(owner:{json.dumps(owner)},name:{json.dumps(repo)}) '
                     f'{{ pullRequest(number:{number}) {{ {fields} }} }}')
    done = subprocess.run(["gh", "api", "graphql", "--input", "-"],
                          input=json.dumps({"query": "query { " + " ".join(parts) + " }"}),
                          capture_output=True, text=True, timeout=20, check=False)
    body = json.loads(done.stdout)
    if not isinstance(body.get("data"), dict):
        raise ValueError("GraphQL query returned no data")
    errors = body.get("errors") or []
    if any(not e.get("path") for e in errors):
        raise ValueError("GraphQL query failed without target-specific errors")
    result = {}
    for index, target in enumerate(targets):
        repo = body.get("data", {}).get(f"p{index}") or {}
        pr = repo.get("pullRequest")
        target_errors = [e.get("message", "GraphQL error") for e in errors if e.get("path", [None])[0] == f"p{index}"]
        if not pr or target_errors:
            result[target] = {"error": "; ".join(target_errors) or "PR is missing or inaccessible"}
            continue
        commits = (pr.get("commits") or {}).get("nodes") or []
        commit = commits[-1].get("commit", {}) if commits else {}
        contexts = (commit.get("statusCheckRollup") or {}).get("contexts") or {}
        checks = [(c.get("status") or c.get("state"), c.get("name") or c.get("context"),
                   c.get("conclusion"), c.get("completedAt") or c.get("createdAt"), pr.get("headRefOid"))
                  for c in contexts.get("nodes", [])]
        # A partial check set cannot prove all checks finished.
        if contexts.get("pageInfo", {}).get("hasNextPage"):
            checks.append(("PENDING", "more checks", None, None, pr.get("headRefOid")))
        result[target] = {"state": pr["state"], "merged_at": pr.get("mergedAt"), "closed_at": pr.get("closedAt"), "checks": [list(c) for c in sorted(checks, key=str)],
                          "reviews": sorted(r["id"] for r in pr.get("reviews", {}).get("nodes", []) if r.get("submittedAt")),
                          "review_times": {r["id"]: r["submittedAt"] for r in pr.get("reviews", {}).get("nodes", []) if r.get("submittedAt")}}
    return result
