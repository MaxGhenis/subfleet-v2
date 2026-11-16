"""Durable completion wakes for quiet, ordinary callers (C-15.9).

The sessions kit supplies eligibility and the existing inbox transport supplies
delivery. Claims commit before socket I/O. An interrupted or ambiguous send is
terminal: this inbox cannot deduplicate a repeated message after a crash.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import datetime, timezone
import os
import time
from time import monotonic as _monotonic

from . import notice_claims, notify_push, protocol, render
from .sessions import nudge, transcripts

RETRY_WINDOW_S = 24 * 60 * 60
RETRY_MAX_S = 300
#: Refusals proved to come before any message byte. The notice goes back to
#: unread, where the prompt hooks still show it, and retries on the backoff.
#: `push_to_session` refuses a lane session before it reads a token or opens a
#: socket (review r1, finding 4).
PREFLIGHT_FAILURES = frozenset({"no-inbox-socket", "no-peer-token", notify_push.LANE_REFUSAL})
#: No registry row, or one naming a dead pid: an app restart or an account
#: switch looks like this. These retry on the same clock and end undeliverable
#: only when it runs out (review r1, finding 6).
CLOSED_FAILURES = frozenset({"session-not-registered", "session-not-running"})
#: Callers one pass examines, woken or not (review r1, finding 8). The next pass
#: starts after the last caller this one examined, so every caller is reached.
MAX_GROUPS_PER_PASS = 32
#: A caller found held is not judged again (no transcript tail read, no session
#: facts) until its notices, registry row, transcript file or wake clock change,
#: its earliest time gate lapses, or this long has passed.
HELD_RECHECK_S = 60.0
#: Judge a held caller again this much before a time gate lapses: the gate is
#: decided on microsecond datetimes, the memo on float seconds.
_GATE_MARGIN_S = 1e-3
#: `wake_reason` of a notice already older than the retry window at its first
#: attempt (review r1, finding 3). Its `state` stays `pending` for the hooks.
STALE_REASON = "older-than-retry-window"
_BATCH = 500
#: The transport a PostToolUse claim records (C-15.2 layer 2, C-15.3).
POST_TOOL_USE = "hook:PostToolUse"
#: At most this long, within its own `deadline_s`, a PostToolUse claim waits for
#: the caller's delivery lock (review r2 of #307, P1 1). A pass holds the lock
#: while it judges the caller and through a wake's send, a prompt hook while it
#: claims, an offline hook while it writes its receipt. A claim that gave up at
#: once would leave the result to a wake five quiet minutes later.
CLAIM_LOCK_WAIT_S = 2.0


def _instant(stamp: str) -> float:
    value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()


def _instant_or_none(stamp) -> float | None:
    try:
        return _instant(stamp) if isinstance(stamp, str) else None
    except (ValueError, OverflowError):
        return None


def _deadline(row: dict, now: float) -> float:
    """When a notice's retry window ends; an unreadable creation time has none left."""
    try:
        return _instant(row["created_at"]) + RETRY_WINDOW_S
    except (ValueError, TypeError, OverflowError, AttributeError):
        return now


def _stat(path) -> tuple | None:
    try:
        info = os.stat(path) if path else None
    except OSError:
        return None
    return None if info is None else (info.st_ino, info.st_size, info.st_mtime_ns)


def _abandoned(args: protocol.NoticePendingArgs, arrived: float | None, client_gone) -> bool:
    """Whether nobody is left to print what a surface claim would return.

    The hook's client gives up after its own timeout and prints offline, so a
    claim that runs later, queued on the request pool, must mark nothing
    (review r1, finding 5). `deadline_s` counts from `arrived`, when the daemon
    read the request, as `wait`'s deadline does (C-15.4).
    """
    if client_gone is not None and client_gone():
        return True
    budget = args.deadline_s
    if arrived is None or isinstance(budget, bool) or not isinstance(budget, (int, float)):
        return False
    return _monotonic() >= arrived + budget


def _lock_wait(args: protocol.NoticePendingArgs, arrived: float | None) -> float:
    """How long a PostToolUse claim may wait for the delivery lock: at most
    `CLAIM_LOCK_WAIT_S`, and never past its own `deadline_s`."""
    budget = args.deadline_s
    if arrived is None or isinstance(budget, bool) or not isinstance(budget, (int, float)):
        return CLAIM_LOCK_WAIT_S
    return max(0.0, min(CLAIM_LOCK_WAIT_S, arrived + budget - _monotonic()))


def reserve(tx, session: str, rows: list[dict], *, transport: str, at,
            wake: bool = False) -> list[dict]:
    """C-15.9: the one reservation every path takes before it delivers a notice.

    A notice leaves `pending` once, in one conditional update matched on its full
    identity; the path whose update matched delivers it, and every other path
    delivers nothing (review r2 of #307, P1 1). The wake marks it `surfaced` and
    claimed, and also needs `wake_state` NULL, so a stale or expired notice is never
    woken, though a hook may still show it. PostToolUse marks it `offered`: the
    harness acknowledges nothing, so layer 3 shows it again (C-15.2, C-15.3).
    Returns the rows this call reserved.
    """
    won = []
    for row in rows:
        identity = (row["notice_id"], row["job_id"], session, row["created_at"], row["text"])
        if wake:
            cursor = tx.execute(
                "UPDATE notices SET state='surfaced',transport=?,wake_state='claimed',"
                "wake_attempts=wake_attempts+1,wake_at=?,wake_next_at=NULL "
                "WHERE notice_id=? AND job_id=? AND session_id=? AND created_at=? AND text=? "
                "AND state='pending' AND wake_state IS NULL", (transport, at, *identity))
        else:
            cursor = tx.execute(
                "UPDATE notices SET state='offered',transport=?,offered_at=COALESCE(offered_at,?) "
                "WHERE notice_id=? AND job_id=? AND session_id=? AND created_at=? AND text=? "
                "AND state='pending'", (transport, at, *identity))
        if cursor.rowcount:
            won.append(row)
    return won


def _record_activity(tx, session: str, now: float, event: str, source, personal: bool) -> None:
    """A session hook's activity: the quiet clock, the SessionStart source, and for
    personal input the reset of the unattended bounds."""
    tx.execute("INSERT INTO caller_wake_sessions(session_id,active_at,source) VALUES(?,?,?) "
               "ON CONFLICT(session_id) DO UPDATE SET active_at=excluded.active_at,source=excluded.source,"
               "wake_streak=CASE WHEN ? THEN 0 ELSE wake_streak END,"
               "last_wake_at=CASE WHEN ? THEN NULL ELSE last_wake_at END",
               (session, now, source if event == "SessionStart" else None, personal, personal))


def _post_tool_use(daemon, args: protocol.NoticePendingArgs, *, arrived: float | None,
                   client_gone) -> dict:
    """C-15.2 layer 2's claim of one job's notices, before the hook prints them.

    Taken as the wake takes its own (review r2 of #307, P1 1): under the caller's
    delivery lock, after the offline receipts are reconciled, through `reserve`.
    The answer's `notices` are the rows this claim reserved, and the only ones the
    hook may print. `absent` is true only when this session has no notice for the
    job at all, so no other path can deliver it and the hook may print its line
    from the job row (C-15.1). A claim that cannot take the lock in time, or whose
    client has gone or deadline passed, reserves nothing and is not `absent`.
    With `release`, the hook gives back the `notice_ids` it reserved but could not
    print: they are `pending` again, for the wake and the prompt hooks, unless
    another path has moved them on since.
    """
    session, job_id = args.session_id, args.job_id
    if not isinstance(job_id, str) or not job_id.strip():
        raise protocol.request_error("invalid-args")
    if args.release is True:
        ids = [protocol.notice_row(notice_id) for notice_id in args.notice_ids]
        released = []
        with daemon.store.transaction("notice.hook_released", data={"session_id": session,
                                                                    "job_id": job_id}) as tx:
            for table, row_id in ids:
                if table == protocol.JOB_NOTICE_TABLE and tx.execute(
                        "UPDATE notices SET state='pending',transport=NULL,offered_at=NULL "
                        "WHERE notice_id=? AND session_id=? AND job_id=? AND state='offered' AND transport=?",
                        (row_id, session, job_id, POST_TOOL_USE)).rowcount:
                    released.append(row_id)
        return {"notices": [], "released": released}
    with notice_claims.delivery_lock(daemon.root, session, wait_s=_lock_wait(args, arrived),
                                     gone=client_gone) as held:
        if not held:
            return {"notices": [], "absent": False}
        notice_claims.reconcile(daemon, session)
        with daemon.store.transaction("notice.hook_claimed", data={"session_id": session, "event": "PostToolUse",
                                                                   "job_id": job_id}) as tx:
            rows = [dict(row) for row in tx.execute(
                "SELECT * FROM notices WHERE session_id=? AND job_id=? ORDER BY notice_id", (session, job_id))]
            if _abandoned(args, arrived, client_gone):
                daemon.store.audit_as("notice.hook_abandoned", {"session_id": session, "event": "PostToolUse"})
                return {"notices": [], "absent": False}
            stamp = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()
            won = reserve(tx, session, [row for row in rows if row["state"] == "pending"],
                          transport=POST_TOOL_USE, at=stamp)
    return {"notices": [{**row, "state": "offered", "transport": POST_TOOL_USE} for row in won],
            "absent": not rows}


def surface(daemon, args: protocol.NoticePendingArgs, *, arrived: float | None = None,
            client_gone=None) -> dict:
    """Reserve the hook's rows and activity atomically, before it prints them.

    Plain notice.pending remains read-only. This opt-in shape preserves older
    clients and notice.ack's wire payload. The hook's later mark is harmless.
    A claim whose client has gone, or whose deadline has passed, still records
    the activity but marks no notice. A prompt that is Subfleet's own inbox
    envelope (`own_envelope`) is activity, not personal input, so it resets no
    unattended bound (review r1, finding 2). A prompt or start that finds the
    delivery lock taken (a pass judging or waking this caller) records its
    activity all the same and claims nothing, so a wake whose claim comes after
    it holds (review r2 of #307, P1 2). PostToolUse claims one job's notices
    (`_post_tool_use`).
    """
    session, event = args.session_id, args.surface
    if event == "PostToolUse":
        return _post_tool_use(daemon, args, arrived=arrived, client_gone=client_gone)
    if event not in ("SessionStart", "UserPromptSubmit"):
        raise protocol.ProtocolError("unknown notice state", reason="notice-state")
    now = time.time()
    personal = event == "UserPromptSubmit" and args.own_envelope is not True
    with notice_claims.delivery_lock(daemon.root, session) as held:
        if not held:
            with daemon.store.transaction("notice.hook_activity", data={"session_id": session,
                                                                        "event": event}) as tx:
                _record_activity(tx, session, now, event, args.source, personal)
            return {"notices": []}
        notice_claims.reconcile(daemon, session)
        with daemon.store.transaction("notice.hook_claimed", data={"session_id": session,
                                                                   "event": event}) as tx:
            _record_activity(tx, session, now, event, args.source, personal)
            rows = [dict(row) for row in tx.execute(
                "SELECT * FROM notices WHERE session_id=? AND state IN ('pending','offered') ORDER BY notice_id",
                (session,))]
            service = [dict(row) for row in tx.execute(
                "SELECT * FROM service_notices WHERE session_id=? AND state IN ('pending','offered') ORDER BY notice_id",
                (session,))]
            about = daemon._pin_notice_jobs(service) if service else {}
            rows += [{**protocol.service_notice_on_wire(row), "job_id": about.get(row["notice_id"])}
                     for row in service]
            if args.jobs_only:
                rows = [row for row in rows if row.get("job_id")]
            if _abandoned(args, arrived, client_gone):
                daemon.store.audit_as("notice.hook_abandoned", {"session_id": session, "event": event})
                return {"notices": []}
            stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
            for row in rows:
                table, row_id = protocol.notice_row(row["notice_id"])
                tx.execute(f"UPDATE {table} SET state='surfaced',transport=?,offered_at=COALESCE(offered_at,?) "
                           "WHERE notice_id=? AND session_id=? AND state IN ('pending','offered')",
                           (f"hook:{event}", stamp, row_id, session))
    return {"notices": rows}


class CallerWakeEngine:
    def __init__(self, daemon, *, now=None):
        self.daemon = daemon
        self.store = daemon.store
        self.now = now or time.time
        #: The last caller a pass examined; the next pass starts after it.
        self._cursor: str | None = None
        #: session -> (inputs, transcript path, its stat, recheck time) of a
        #: caller last found held. Memory only: a restart judges everyone again.
        self._held: dict[str, tuple] = {}
        self.recover()

    def recover(self) -> None:
        """Rescan durable claims on startup; never replay an uncertain send."""
        with self.store.transaction("notice.wake_recovered") as tx:
            tx.execute("UPDATE notices SET wake_state='undeliverable',wake_reason='interrupted-send' "
                       "WHERE wake_state='claimed'")

    def _facts(self, session: str) -> dict:
        result = self.daemon.sessions(protocol.SessionsArgs(action="state", session_ids=[session]))
        return (result.get("sessions") or {}).get(session) or {}

    def _clock(self, session: str) -> dict:
        return self.store.one("SELECT * FROM caller_wake_sessions WHERE session_id=?", (session,)) or {}

    def _eligibility(self, session: str, entry: dict, state, limits: dict, now: float
                     ) -> tuple[bool, str, float]:
        """Whether to wake, why, and the earliest time a hold could lapse unaided."""
        clock = self._clock(session)
        # The app publishes these in milliseconds. Busy remains busy even
        # during a long tool call that writes no new transcript records.
        if entry.get("activity_invalid"):
            return False, "session activity is invalid", now + HELD_RECHECK_S
        if entry.get("status") == "busy":
            return False, "session is busy", now + HELD_RECHECK_S
        facts = self._facts(session)
        active_at = max(clock.get("active_at") or 0,
                        (entry.get("status_updated_at") or 0) / 1000,
                        (entry.get("updated_at") or 0) / 1000)
        allowed, reason = nudge.notice_decide(
            state, limits=limits, active_at=active_at, source=clock.get("source"),
            retired=facts.get("retired"), last_nudge=facts.get("last_nudge"),
            wake_streak=clock.get("wake_streak") or 0, last_wake_at=clock.get("last_wake_at"),
            now=datetime.fromtimestamp(now, timezone.utc))
        return allowed, reason, self._lapse(state, clock, facts, active_at, limits, now)

    @staticmethod
    def _lapse(state, clock: dict, facts: dict, active_at: float, limits: dict, now: float) -> float:
        """The earliest time gate still ahead (quiet, cooldowns, streak), capped."""
        from .conversations.wakes import COOLDOWN_S, MAX_STREAK
        gates = []
        latest = max(_instant_or_none(state.timestamp) or 0, active_at or 0)
        if latest:
            gates.append(latest + limits["notice_quiet_s"])
        last_wake = clock.get("last_wake_at")
        if last_wake is not None:
            gates.append(last_wake + limits["cooldown_s"])
            if (clock.get("wake_streak") or 0) >= MAX_STREAK:
                gates.append(last_wake + COOLDOWN_S)
        nudged = _instant_or_none((facts.get("last_nudge") or {}).get("at"))
        if nudged is not None:
            gates.append(nudged + limits["cooldown_s"])
        return min([now + HELD_RECHECK_S] + [gate - _GATE_MARGIN_S for gate in gates if gate > now])

    def _held_key(self, session: str, entry: dict, rows: list[dict]) -> tuple:
        """Everything a held caller's judgement reads, except the transcript and the clock time."""
        clock = self._clock(session)
        return (tuple(sorted((row["notice_id"], row["created_at"]) for row in rows)),
                tuple(entry.get(key) for key in ("pid", "alive", "socket_present", "socket", "status",
                                                 "status_updated_at", "updated_at", "activity_invalid",
                                                 "started_at")),
                tuple(clock.get(key) for key in ("active_at", "source", "wake_streak", "last_wake_at")))

    def _current(self, session: str, candidates: list[dict]) -> list[dict]:
        """The candidates still unread and unclaimed, by full identity, in one read per batch."""
        live = set()
        for start in range(0, len(candidates), _BATCH):
            batch = candidates[start:start + _BATCH]
            for row in self.store.query(
                    "SELECT notice_id,job_id,created_at,text FROM notices WHERE session_id=? "
                    "AND state='pending' AND wake_state IS NULL "
                    f"AND notice_id IN ({','.join('?' * len(batch))})",
                    (session, *(row["notice_id"] for row in batch))):
                live.add((row["notice_id"], row["job_id"], row["created_at"], row["text"]))
        return [row for row in candidates
                if (row["notice_id"], row["job_id"], row["created_at"], row["text"]) in live]

    def _stale(self, rows: list[dict], now: float) -> None:
        """First attempt after the retry window: never wake; the hooks still show it."""
        with self.store.transaction("notice.wake_stale", data={"notices": len(rows)}) as tx:
            tx.executemany("UPDATE notices SET wake_state='stale',wake_reason=?,wake_at=? "
                           "WHERE notice_id=? AND job_id=? AND session_id IS ? AND created_at=? AND text=? "
                           "AND state='pending' AND wake_state IS NULL AND wake_attempts=0",
                           [(STALE_REASON, now, row["notice_id"], row["job_id"], row["session_id"],
                             row["created_at"], row["text"]) for row in rows])

    def _failure(self, rows: list[dict], reason: str, now: float, *, retry: bool) -> None:
        with self.store.transaction("notice.wake_failed", data={"reason": reason}) as tx:
            for row in rows:
                deadline = _deadline(row, now)
                again = retry and now < deadline
                attempts = row["wake_attempts"] + 1
                next_at = min(deadline, now + min(RETRY_MAX_S, 2 ** min(attempts, 9))) if again else None
                tx.execute("UPDATE notices SET wake_state=?,wake_attempts=?,wake_next_at=?,wake_at=?,wake_reason=? "
                           "WHERE notice_id=? AND job_id=? AND session_id IS ? AND created_at=? AND text=? "
                           "AND state='pending' AND wake_state IS NULL",
                           (None if again else "undeliverable", attempts, next_at, now,
                            reason if again or not retry else "retry-expired", row["notice_id"], row["job_id"],
                            row["session_id"], row["created_at"], row["text"]))

    def pass_once(self) -> dict:
        policy = self.daemon.policy
        limits = nudge.caps(policy)
        result = {"delivered": 0, "sessions": 0, "examined": 0}
        if not policy.get("sessions", {}).get("notice_wake_enabled", True) or not nudge.enabled():
            return result
        now = self.now()
        # `offered` is left out: PostToolUse (layer 2) or `wait` already showed it
        # to the caller, and the prompt hooks still show it (review r1, finding 1).
        due, stale, expired = [], [], []
        for row in self.store.query(
                "SELECT n.* FROM notices n JOIN jobs j USING(job_id) "
                "WHERE n.job_id IS NOT NULL AND n.state='pending' AND n.wake_state IS NULL "
                "AND j.kind<>'turn' AND (n.wake_next_at IS NULL OR n.wake_next_at<=?) "
                "ORDER BY n.created_at,n.notice_id", (now,)):
            if now < _deadline(row, now):
                due.append(row)
            elif row["wake_attempts"]:
                expired.append(row)
            else:
                stale.append(row)
        if stale:
            self._stale(stale, now)
        if expired:
            self._failure(expired, "retry-expired", now, retry=False)
        groups = defaultdict(list)
        for row in due:
            groups[row["session_id"] or ""].append(row)
        self._held = {session: memo for session, memo in self._held.items() if session in groups}
        order = sorted(groups)
        start = bisect.bisect_right(order, self._cursor) if self._cursor is not None else 0
        for session in order[start:] + order[:start]:
            if result["sessions"] >= limits["max_sessions"] or result["examined"] >= MAX_GROUPS_PER_PASS:
                break
            result["examined"] += 1
            self._cursor = session
            self._wake(session, groups[session], limits, now, result)
        return result

    def _wake(self, session: str, candidates: list[dict], limits: dict, now: float, result: dict) -> None:
        if not session:
            self._failure(candidates, "no-caller-session", now, retry=False)
            return
        if self.daemon._conversation_binding(session):
            return  # C-24.10 is the only wake path for this session.
        with notice_claims.delivery_lock(self.daemon.root, session) as held:
            if not held:
                return
            try:
                notice_claims.reconcile(self.daemon, session)
            except (OSError, ValueError, KeyError):
                return  # a bad receipt fences only its own caller
            # Hooks and other passes may have consumed the candidate read.
            rows = self._current(session, candidates)
            if not rows:
                return
            entry = notify_push.find_session(session)
            if entry is None or not entry["alive"]:
                self._failure(rows, "session-not-registered" if entry is None else "session-not-running",
                              now, retry=True)
                return
            key = self._held_key(session, entry, rows)
            memo = self._held.pop(session, None)
            if memo is not None and memo[0] == key and now < memo[3] and _stat(memo[1]) == memo[2]:
                self._held[session] = memo
                return  # known held, and nothing it was judged on has changed
            path = transcripts.transcript_path(session)
            stat = _stat(path)
            before = transcripts.turn_state(path, now=datetime.fromtimestamp(now, timezone.utc))
            allowed, _reason, until = self._eligibility(session, entry, before, limits, now)
            if not allowed:
                self._held[session] = (key, path, stat, until)
                return
            if not entry["socket_present"]:
                self._failure(rows, "no-inbox-socket", now, retry=True)
                return
            after = transcripts.turn_state(path, now=datetime.fromtimestamp(now, timezone.utc))
            if before.fingerprint != after.fingerprint:
                return
            # Account switches replace the registry row; a fresh busy
            # status can also arrive without changing the transcript.
            entry = notify_push.find_session(session)
            if entry is None or not entry["alive"]:
                self._failure(rows, "session-not-registered" if entry is None else "session-not-running",
                              now, retry=True)
                return
            if not entry["socket_present"]:
                self._failure(rows, "no-inbox-socket", now, retry=True)
                return
            # Only header fields are read for the wake. Notice summaries,
            # attempts' output and deliverable file contents never enter it.
            # Rendered before the claim, so nothing between the claim's commit
            # and the send can raise and leave a claim behind.
            try:
                headers = {row["notice_id"]: render.notice_header(self.store.get_job(row["job_id"]),
                                                                  self.daemon.root) for row in rows}
            except Exception:                           # noqa: BLE001 - retried, never sent
                self._failure(rows, "header-unavailable", now, retry=True)
                return
            try:
                # Publication and the final claim share a short lock: an offline
                # hook that completed before this claim must be visible here,
                # even though this pass held delivery_lock during its prompt.
                # No files are consumed here, so a rollback cannot lose input.
                with notice_claims.activity_lock(self.daemon.root, session), self.store.transaction(
                        "notice.wake_claimed", data={"session_id": session}) as tx:
                    notice_claims.apply_activity(tx, session, notice_claims.read_activity(self.daemon.root, session))
                    # Judge the registry, transcript and activity again after rendering.
                    if self.daemon._conversation_binding(session):
                        return
                    entry = notify_push.find_session(session)
                    if entry is None or not entry["alive"] or not entry["socket_present"]:
                        return
                    current = transcripts.turn_state(path, now=datetime.fromtimestamp(now, timezone.utc))
                    if current.fingerprint != before.fingerprint or not self._eligibility(
                            session, entry, current, limits, now)[0]:
                        return
                    previous_clock = self.store.one(
                        "SELECT wake_streak,last_wake_at FROM caller_wake_sessions WHERE session_id=?", (session,)) or {}
                    claimed = reserve(tx, session, rows, transport="caller-wake", at=now, wake=True)
                    if claimed:
                        tx.execute("INSERT INTO caller_wake_sessions(session_id,last_wake_at,wake_streak) VALUES(?,?,1) "
                                   "ON CONFLICT(session_id) DO UPDATE SET last_wake_at=excluded.last_wake_at,"
                                   "wake_streak=wake_streak+1", (session, now))
            except (OSError, ValueError, KeyError):
                return  # invalid activity arriving during render fences only its caller
            if not claimed:
                return
            result["sessions"] += 1
            try:
                outcome = notify_push.push_to_session(session, "\n".join(headers[row["notice_id"]] for row in claimed),
                                                      lane_sessions=self.daemon._lane_session_ids())
            except Exception:
                outcome = {"delivered": False, "reason": "send-uncertain"}
            if outcome.get("delivered"):
                with self.store.transaction("notice.woken", data={"session_id": session,
                                                                   "notice_ids": [r["notice_id"] for r in claimed]}) as tx:
                    tx.executemany("UPDATE notices SET wake_state='delivered',wake_reason=NULL "
                                   "WHERE notice_id=? AND job_id=? AND session_id=? AND created_at=? AND text=? "
                                   "AND wake_at=? AND wake_state='claimed'",
                                   [(row["notice_id"], row["job_id"], session, row["created_at"], row["text"], now)
                                    for row in claimed])
                result["delivered"] += len(claimed)
                return
            retryable = outcome.get("retryable") is True
            reason = outcome.get("reason")
            if isinstance(reason, str) and reason.split(":", 1)[0] == notify_push.LANE_REFUSAL:
                reason = notify_push.LANE_REFUSAL      # fixed words; the rest is advice
            # Connect failures prove no message bytes were attempted.
            # Persist fixed words, never the transport exception text.
            reason = "inbox-unavailable" if retryable else reason
            safe = retryable or reason in PREFLIGHT_FAILURES or reason in CLOSED_FAILURES
            with self.store.transaction("notice.wake_unsent") as tx:
                for row in claimed:
                    tx.execute("UPDATE notices SET state=?,transport=?,wake_state=?,wake_reason=? "
                               "WHERE notice_id=? AND job_id=? AND session_id=? AND created_at=? AND text=? "
                               "AND wake_at=? AND wake_state='claimed'",
                               (row["state"] if safe else "surfaced", row["transport"] if safe else "caller-wake",
                                None if safe else "undeliverable", reason if safe else "send-uncertain",
                                row["notice_id"], row["job_id"], session, row["created_at"], row["text"], now))
                if safe:
                    # No bytes were attempted. Give back the session
                    # slot too, without undoing newer human activity.
                    tx.execute("UPDATE caller_wake_sessions SET wake_streak=?,last_wake_at=? "
                               "WHERE session_id=? AND last_wake_at=?",
                               (previous_clock.get("wake_streak") or 0, previous_clock.get("last_wake_at"),
                                session, now))
            if safe:
                self._failure(claimed, reason, now, retry=True)
