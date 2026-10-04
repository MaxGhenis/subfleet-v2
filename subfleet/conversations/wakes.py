"""Durable, bounded conversation check-backs (C-24.10).

The control loop owns polling; requests never invoke gh. Acceptance, consumption,
and the unattended counter share the message transaction, including after restart.
"""
from __future__ import annotations

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
CREATE TABLE IF NOT EXISTS wake_meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
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


def trailing_requests(text: str) -> list[dict]:
    """Only consecutive trailing WAKE-ME lines; shell quoting for note values.

    WAKE-ME: runs=ID[,ID...] prs=OWNER/REPO#N[,OWNER/REPO#N...] at=ISO note="TEXT"
    Fields are optional, unique, whitespace separated; at least one trigger.
    """
    lines = []
    for line in reversed(text.rstrip().splitlines()):
        if not line.startswith("WAKE-ME:"):
            break
        lines.append(line)
    requests = []
    for line in reversed(lines):
        fields = {}
        for token in shlex.split(line[len("WAKE-ME:"):]):
            key, sep, value = token.partition("=")
            if not sep or key not in ("runs", "prs", "at", "note") or key in fields or not value:
                raise ConversationError("bad-wake", "invalid WAKE-ME field")
            fields[key] = value
        requests.append({"runs": fields["runs"].split(",") if "runs" in fields else None,
                         "prs": fields["prs"].split(",") if "prs" in fields else None,
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


def claim(tx, cid: str, mid: str, data: dict) -> None:
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
    for kind, request_id in data["requests"]:
        # Kinds in one request are alternatives: the first ready trigger consumes
        # all of them. Independent requests ready this cycle share one message.
        tx.execute("UPDATE wake_requests SET state='fired',message_id=? WHERE conversation_id=? AND request_id=? AND state='pending'",
                   (mid, cid, request_id))
    tx.executemany("INSERT INTO wake_runs VALUES(?,?,?)", [(cid, job_id, mid) for job_id in data["runs"]])
    tx.execute("UPDATE conversations SET wake_streak=wake_streak+1,last_wake_at=? WHERE conversation_id=?", (data["now"], cid))


class WakeEngine:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.now = time.time

    def register(self, cid: str, request_id: str, spec: dict) -> dict:
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
            for kind, payload in spec.items():
                encoded = json.dumps(payload, sort_keys=True)
                previous = tx.execute("SELECT payload_json FROM wake_requests WHERE conversation_id=? AND request_id=? AND kind=?",
                                      (cid, request_id, kind)).fetchone()
                if previous:
                    if previous[0] != encoded:
                        raise ConversationError("wake-id-conflict", "request id already has another payload")
                    continue
                tx.execute("UPDATE wake_requests SET state='superseded' WHERE conversation_id=? AND kind=? AND state='pending'", (cid, kind))
                tx.execute("INSERT INTO wake_requests(conversation_id,request_id,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                           (cid, request_id, kind, encoded, datetime.fromtimestamp(self.now(), UTC).isoformat()))
        return {"request_id": request_id, "kinds": list(spec)}

    def from_final(self, cid: str, mid: str, text: str) -> None:
        try:
            for index, args in enumerate(trailing_requests(text)):
                request_id = f"final:{mid}:{index}"
                # Reconciliation can replay days later: don't revalidate an accepted timer.
                if self.store.one("SELECT 1 FROM wake_requests WHERE conversation_id=? AND request_id=?", (cid, request_id)):
                    continue
                self.register(cid, request_id, normalize(**args, now=self.now()))
        except (ConversationError, ValueError) as exc:
            self.service.log.warning("wake request in final text of %s refused: %s", mid, exc)

    def _completions(self) -> dict[str, list[dict]]:
        conversations = self.store.query("SELECT * FROM conversations")
        by_id = {c["conversation_id"]: c for c in conversations}
        by_session = {canonical_native(c["native_session_id"]): c for c in conversations if c["native_session_id"]}
        result = {}
        # The job's caller is the same session resolved by runs --mine and notices.
        for job in self.service.daemon.store.query(
                "SELECT j.job_id,j.caller_session,j.state,j.out_path,j.created_at,p.name parent_name "
                "FROM jobs j LEFT JOIN jobs p ON j.parent_job_id=p.job_id AND p.kind='turn' WHERE j.kind<>'turn' "
                "AND j.state IN ('succeeded','failed','cancelled','lost','quarantined') "
                "AND NOT EXISTS (SELECT 1 FROM notices n WHERE n.job_id=j.job_id "
                "AND n.state IN ('surfaced','acknowledged') AND COALESCE(n.transport,'')<>'conversation')"):
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
        last = self.store.one("SELECT value FROM wake_meta WHERE key='pr-polled'")
        if last and now - last["value"] < PR_INTERVAL_S:
            return
        with self.store.transaction() as tx:
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
            changed = [p for p in watched if p in snapshots and pr_changed(before.get(p), snapshots[p])]
            observed = {**before, **{p: snapshots[p] for p in watched if p in snapshots}}
            with self.store.transaction() as tx:
                tx.execute("UPDATE wake_requests SET observed_json=?,ready_json=? WHERE conversation_id=? AND request_id=? "
                           "AND kind='pr' AND state='pending'", (json.dumps(observed), json.dumps(changed) if changed else None,
                                                               r["conversation_id"], r["request_id"]))

    def tick(self) -> None:
        self._surface_notices()
        pending = self.store.query("SELECT * FROM wake_requests WHERE state='pending'")
        self._poll_prs(pending)
        pending = self.store.query("SELECT * FROM wake_requests WHERE state='pending'")
        completions = self._completions()
        grouped = {}
        for r in pending:
            grouped.setdefault(r["conversation_id"], []).append(r)
        for cid in grouped.keys() | completions.keys():
            now = self.now()
            with self.store.transaction() as tx:
                if not eligible(tx, cid, now):
                    continue
            # Job leases and finalization may lag the message's terminal receipt.
            if self.service.daemon.store.one("SELECT 1 FROM jobs WHERE kind='turn' AND name=? "
                    "AND state NOT IN ('succeeded','failed','cancelled','lost')", (f"turn-{cid}",)):
                continue
            if self.service.daemon.store.one("SELECT 1 FROM leases WHERE lease_key=?", (f"conversation:{cid}",)):
                continue
            runs = {j["job_id"]: j for j in completions.get(cid, [])}
            ready, notes = [], []
            for r in grouped.get(cid, []):
                payload = json.loads(r["payload_json"])
                kind = r["kind"]
                if kind == "runs":
                    jobs = [self.service.daemon.store.one("SELECT * FROM jobs WHERE job_id=?", (j,)) for j in payload["targets"]]
                    if not all(j and j["state"] in ('succeeded','failed','cancelled','lost','quarantined') for j in jobs):
                        continue
                    runs.update({j["job_id"]: j for j in jobs if not self.store.one(
                        "SELECT 1 FROM wake_runs WHERE conversation_id=? AND job_id=?", (cid, j["job_id"]))})
                elif kind == "time":
                    if now < payload["at"]:
                        continue
                    notes.append("Scheduled check-back is due.")
                elif not r["ready_json"]:
                    continue
                else:
                    notes.append("PR state changed: " + ", ".join(json.loads(r["ready_json"])))
                ready.append((kind, r["request_id"]))
                if payload["note"]:
                    notes.append(payload["note"])
            if not runs and not ready:
                continue
            text = "[Subfleet]\n" + "\n".join(
                [f"{j['job_id']} finished: {j['state']}; deliverable {j['out_path'] or '(none)'}" for j in runs.values()] + notes)
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
        """A durable wake carries the notice, so hooks don't repeat its body.
        Separate stores: a crash before this update is repaired on the next tick.
        """
        ids = [r["job_id"] for r in self.store.query("SELECT DISTINCT job_id FROM wake_runs")]
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            with self.service.daemon.store.transaction("conversation.wake-notices") as tx:
                tx.execute(f"UPDATE notices SET state='surfaced',transport='conversation',offered_at=? "
                           f"WHERE job_id IN ({','.join('?' for _ in chunk)}) AND state='pending'", (utcnow(), *chunk))


def pr_changed(before: dict | None, after: dict) -> bool:
    if before is None:
        checks = after.get("checks", [])
        return after.get("state") in ("MERGED", "CLOSED") or bool(
            checks and all(c[0] in ("COMPLETED", "SUCCESS", "FAILURE", "ERROR") for c in checks))
    if before.get("state") != after.get("state") and after.get("state") in ("MERGED", "CLOSED"):
        return True
    reviews = after.get("reviews", [])
    if any(r not in before.get("reviews", []) for r in reviews):
        return True
    checks = after.get("checks", [])
    return bool(checks and checks != before.get("checks") and all(c[0] in ("COMPLETED", "SUCCESS", "FAILURE", "ERROR") for c in checks))


def query_prs(targets: list[str]) -> dict:
    fields = """state headRefOid commits(last:1) { nodes { commit { statusCheckRollup { contexts(first:100) { nodes {
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
                          capture_output=True, text=True, timeout=20, check=True)
    body = json.loads(done.stdout)
    if body.get("errors"):
        raise ValueError("GraphQL query returned errors")
    result = {}
    for index, target in enumerate(targets):
        repo = body.get("data", {}).get(f"p{index}") or {}
        pr = repo.get("pullRequest")
        if not pr:
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
        result[target] = {"state": pr["state"], "checks": [list(c) for c in sorted(checks, key=str)],
                          "reviews": sorted(r["id"] for r in pr.get("reviews", {}).get("nodes", []) if r.get("submittedAt"))}
    return result
