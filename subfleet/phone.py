"""Needs-you Telegram cards, delivered only through the chief-of-staff say gateway.

The daemon schedules a single worker; socket and dispatch threads never wait on
Telegram. Sending has a durable claim because an interrupted decide delivery
cannot safely be retried. Resolutions and question progress edit known cards.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path

from .conversations import redact
from .conversations.store import utcnow

DEFAULT_EVENTS = ("approvals", "questions", "blocks")
PERSON_BLOCKS = frozenset({"delivery-unknown", "unfinished-turn", "quarantined-turn"})
DECISIONS = {"allow": "Allow", "allow-session": "Allow for session", "allow-turn": "Allow for turn",
             "deny": "Deny", "cancel-turn": "Cancel turn"}
CARD_MAX = 3500                    # UTF-8 bytes; safely below Telegram's UTF-16 cap too
SAY_TIMEOUT = 10.0
TICK_INTERVAL = 1.0
BATCH_SIZE = 16


def settings(policy: dict) -> dict:
    value = (policy.get("phone") or {}).get("telegram") or {}
    return {"enabled": value.get("enabled", True), "events": value.get("events", list(DEFAULT_EVENTS))}


def enabled(policy: dict) -> bool:
    return settings(policy)["enabled"] is True


def _text(value, limit: int = 700) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False) if value is not None else ""
    # Leave line breaks but remove invisible control characters in names/options.
    return redact.bounded("".join(c for c in value if c in "\n\t" or ord(c) >= 32), limit)


def question_actions(approval: dict) -> dict:
    actions = {}
    questions = approval.get("display", {}).get("questions") or []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        for number, option in enumerate(question.get("options") or []):
            label = option.get("label") if isinstance(option, dict) else option
            if isinstance(label, str) and label:
                actions[f"q{index}o{number}"] = {"kind": "option", "question": index, "option": label}
        if question.get("multiSelect"):
            actions[f"q{index}done"] = {"kind": "finish", "question": index}
    return actions


def offered_actions(approval: dict) -> dict:
    actions = question_actions(approval) if approval["kind"] == "question" else {}
    for index, decision in enumerate(approval.get("options") or []):
        if decision != "answer":
            actions[f"a{index}"] = {"kind": "decision", "decision": decision}
    return actions


def run_say(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Kill the gateway's process group on timeout, including secret/fallback children."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate(timeout=1)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


class PhoneBridge:
    def __init__(self, service, *, say: str | None = None, timeout: float = SAY_TIMEOUT):
        self.service = service
        self.store = service.store
        self.say = say or os.environ.get("SUBFLEET_SAY") or str(Path.home() / "chief-of-staff/bin/say")
        self.timeout = timeout
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="subfleet-phone")
        self._schedule = threading.Lock()
        self._reconciling = threading.Lock()
        self._future = None
        self._next_tick = 0.0
        self._closed = False

    def tick(self) -> None:
        with self._schedule:
            now = time.monotonic()
            if self._closed or now < self._next_tick or (self._future and not self._future.done()):
                return
            self._next_tick = now + TICK_INTERVAL
            self._future = self._pool.submit(self._background)

    def _background(self) -> None:
        try:
            self.reconcile()
        except Exception as exc:
            self.service.log.warning("phone reconciliation failed: %s", _text(str(exc), 300))

    def close(self) -> None:
        with self._schedule:
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)
        # Also joins synchronous reconcile() callers. No write can outlive close.
        with self._reconciling:
            pass

    def reconcile(self) -> None:
        with self._reconciling:
            if self._closed:
                return
            # A prior process may have sent these. Never guess and send again.
            interrupted = self.store.query("SELECT token FROM phone_cards WHERE state='sending'")
            with self.store.transaction() as tx:
                tx.execute("UPDATE phone_cards SET state='unknown', error='interrupted send; delivery unknown', "
                           "updated_at=? WHERE state='sending'", (utcnow(),))
            for card in interrupted:
                self.service.log.warning("phone card %s delivery unknown after interrupted send", card["token"])
            self._refresh_sent()
            policy = settings(self.service.daemon.policy)
            if self._closed or not policy["enabled"]:
                return
            self._discover(set(policy["events"]))
            handled = 0
            for card in self.store.query("SELECT * FROM phone_cards WHERE state='pending' ORDER BY created_at"):
                if self._closed:
                    return
                # Turning an event off also holds its unclaimed old deliveries.
                if card["kind"] not in policy["events"]:
                    continue
                if handled >= BATCH_SIZE:
                    return
                handled += 1
                self._send(card)

    def _discover(self, events: set[str]) -> None:
        for approval in self.store.approvals():
            kind = "questions" if approval["kind"] == "question" else "approvals"
            if kind not in events:
                continue
            conversation = self.store.conversation(approval["conversation_id"])
            if not conversation.get("archived_at"):
                self._create(f"approval:{approval['approval_id']}", kind, conversation["conversation_id"],
                             approval["message_id"], approval=approval)
        if "blocks" in events:
            for conversation in self.store.query("SELECT * FROM conversations WHERE archived_at IS NULL "
                                                 "AND blocked_by IS NOT NULL"):
                reason = conversation["blocked_by"]
                if reason not in PERSON_BLOCKS:
                    continue
                message = self.store.one("SELECT * FROM messages WHERE conversation_id=? "
                                         "AND state NOT IN ('queued','waiting') ORDER BY seq DESC LIMIT 1",
                                         (conversation["conversation_id"],))
                mid = message["message_id"] if message else None
                turn = message["turn_seq"] if message else 0
                self._create(f"block:{conversation['conversation_id']}:{reason}:{mid}:{turn}", "blocks",
                             conversation["conversation_id"], mid, outcome=reason)
        if "done" in events:
            for subscription in self.store.query("SELECT * FROM phone_notify"):
                message = self.store.one("SELECT * FROM messages WHERE conversation_id=? AND seq>? "
                                         "AND state='complete' ORDER BY seq LIMIT 1",
                                         (subscription["conversation_id"], subscription["after_seq"]))
                if message:
                    self._create(f"done:{message['message_id']}", "done", message["conversation_id"],
                                 message["message_id"])
                    with self.store.transaction() as tx:
                        tx.execute("DELETE FROM phone_notify WHERE conversation_id=? AND after_seq=?",
                                   (subscription["conversation_id"], subscription["after_seq"]))

    def _create(self, event_key: str, kind: str, cid: str, mid: str | None, *, approval=None, outcome=None):
        actions = offered_actions(approval) if approval else {}
        progress = {"index": 0, "answers": {}, "selected": [], "consumed": []}
        now = utcnow()
        with self.store.transaction() as tx:
            tx.execute("INSERT OR IGNORE INTO phone_cards(token,event_key,conversation_id,message_id,approval_id,kind,"
                       "state,actions_json,progress_json,outcome,created_at,updated_at) VALUES (?,?,?,?,?,?,'pending',?,?,?,?,?)",
                       (secrets.token_hex(12), event_key, cid, mid, approval["approval_id"] if approval else None,
                        kind, json.dumps(actions), json.dumps(progress), outcome, now, now))

    def _context(self, card: dict) -> tuple[dict, dict | None, str]:
        conversation = self.store.conversation(card["conversation_id"])
        message = self.store.message(card["message_id"]) if card["message_id"] else None
        served = (message or {}).get("served") or {}
        lane_id = served.get("lane_id") or conversation.get("lane_id")
        account = served.get("account")
        # A live turn's served_json is settled later; its durable attempt already
        # names the actual lane. Do not report merely the preferred lane instead.
        jobs = getattr(self.service.daemon, "store", None)
        if message and message.get("job_id") and jobs:
            attempt = jobs.one("SELECT lane_id FROM attempts WHERE job_id=? ORDER BY seq DESC LIMIT 1",
                               (message["job_id"],))
            if attempt:
                lane_id = attempt["lane_id"] or lane_id
        if lane_id and jobs and not account:
            lane = jobs.one("SELECT label,account_key FROM lanes WHERE lane_id=?", (lane_id,))
            if lane:
                account = lane.get("label") or lane.get("account_key")
        context = f"Lane: {_text(lane_id or 'unassigned', 120)} · Account: {_text(account or 'unassigned', 160)}"
        return conversation, message, context

    def _render(self, card: dict, *, approval: dict | None = None) -> tuple[str, dict]:
        conversation, message, context = self._context(card)
        title = _text(conversation.get("title") or conversation["conversation_id"], 180)
        lines = [f"Subfleet · {title}", context]
        buttons = []
        if approval:
            if approval["state"] != "pending":
                decision = approval.get("decision") or {}
                label = "Answered" if decision.get("decision") == "answer" else DECISIONS.get(
                    decision.get("decision"), decision.get("decision"))
                lines.append("Withdrawn" if approval["state"] == "withdrawn" else f"Resolved: {label or 'answered'}")
                if decision.get("answers"):
                    lines.append(_text(decision["answers"], 1100))
            else:
                display = approval.get("display") or {}
                actions = json.loads(card["actions_json"])
                progress = json.loads(card["progress_json"])
                if approval["kind"] == "question":
                    questions = display.get("questions") or []
                    index = progress.get("index", 0)
                    question = questions[index] if index < len(questions) and isinstance(questions[index], dict) else {}
                    lines += [f"Question {index + 1}/{max(1, len(questions))}", _text(question.get("question") or
                              display.get("input") or "Your answer is needed", 1100)]
                    selected = progress.get("selected") or []
                    if selected:
                        lines.append("Selected: " + _text(", ".join(selected), 600))
                    if question.get("options"):
                        lines.append("Choose options, then Finish." if question.get("multiSelect") else "Choose an option.")
                    lines.append("Reply to answer.")
                else:
                    lines.append("Approval needed")
                    # The summary is already display-safe; scrub once more at the
                    # outbound boundary. Never send request_path, raw request or nonce.
                    excerpt = "\n".join(f"{key}: {_text(value, 800)}" for key, value in display.items()
                                        if value not in (None, "", [], {}) and key != "questions")
                    lines.append(_text(excerpt, 1900))
                for key, action in actions.items():
                    if action["kind"] == "decision":
                        label = DECISIONS.get(action["decision"], action["decision"])
                    elif action.get("question") != progress.get("index", 0):
                        continue
                    elif action["kind"] == "finish":
                        label = "Finish"
                    else:
                        label = ("✓ " if action["option"] in progress.get("selected", []) else "") + action["option"]
                    # Keep callback data opaque and under Telegram's 64-byte cap.
                    callback = f"sf:{card['token']}:{key}"
                    if len(callback.encode()) <= 64:
                        buttons.append([{"text": _text(label, 64), "callback_data": callback}])
                buttons = buttons[:90]
        elif card["kind"] == "blocks":
            lines += ["Needs you: " + _text(card.get("outcome"), 160), "Reply to queue a message; resolve the block in the app."]
        else:
            lines.append("Requested turn completed")
        if not approval and message:
            event = self.store.one("SELECT data_json FROM events WHERE message_id=? AND kind='text' ORDER BY seq DESC LIMIT 1",
                                   (message["message_id"],))
            data = json.loads(event["data_json"]) if event else {}
            excerpt = data.get("text") or self.store.message_text(message)
            lines.append(_text(excerpt, 1100))
        text = _text("\n\n".join(part for part in lines if part), CARD_MAX)
        text = text.encode("utf-8")[:CARD_MAX].decode("utf-8", errors="ignore")
        return text, {"inline_keyboard": buttons}

    @staticmethod
    def _hash(text: str, markup: dict) -> str:
        return hashlib.sha256(json.dumps([text, markup], sort_keys=True).encode()).hexdigest()

    def _update(self, token: str, **fields) -> None:
        # close() joins this reconciliation before closing the store. Record a
        # gateway receipt that arrived while it was joining, so a known send
        # never becomes needlessly ambiguous during an orderly shutdown.
        fields["updated_at"] = utcnow()
        with self.store.transaction() as tx:
            tx.execute("UPDATE phone_cards SET " + ",".join(f"{key}=?" for key in fields) + " WHERE token=?",
                       (*fields.values(), token))
        if fields.get("state") in ("failed", "unknown"):
            self.service.log.warning("phone card %s delivery %s: %s", token, fields["state"], fields.get("error"))

    def _send(self, card: dict) -> None:
        approval = self.store.approval(card["approval_id"]) if card["approval_id"] else None
        if approval and approval["state"] != "pending":
            self._update(card["token"], state="resolved", outcome=approval["state"])
            return
        if card["kind"] == "blocks" and self.store.conversation(card["conversation_id"])["blocked_by"] != card["outcome"]:
            self._update(card["token"], state="resolved")
            return
        text, markup = self._render(card, approval=approval)
        with self.store.transaction() as tx:
            claimed = tx.execute("UPDATE phone_cards SET state='sending',updated_at=? WHERE token=? AND state='pending'",
                                 (utcnow(), card["token"])).rowcount
        if not claimed:
            return
        if self._closed:
            self._update(card["token"], state="pending")
            return
        cls = "decide" if approval else "alert" if card["kind"] == "blocks" else "requested"
        argv = [self.say, "--class", cls, "--key", f"sf:{card['token']}", "--markup", json.dumps(markup), text]
        try:
            result = run_say(argv, self.timeout)
        except FileNotFoundError as exc:
            self._update(card["token"], state="failed", error=_text(str(exc), 400))
            return
        except (OSError, subprocess.SubprocessError) as exc:
            self._update(card["token"], state="unknown", error=_text(str(exc), 400))
            return
        output = result.stdout.strip()
        match = re.fullmatch(r"sent message_id ([1-9][0-9]*)", output)
        if match and result.returncode == 0:
            self._update(card["token"], state="sent", telegram_message_id=int(match[1]),
                         rendered_hash=self._hash(text, markup), error=None)
        else:
            state = next((s for s in ("held", "deduped", "emailed") if output.startswith(s)), "unknown")
            if output.startswith("telegram failed, emailed"):
                state = "emailed"
            self._update(card["token"], state=state, error=_text(output or result.stderr or "no delivery receipt", 400))

    def _refresh_sent(self) -> None:
        cards = self.store.query("SELECT * FROM phone_cards WHERE state='sent' AND approval_id IS NOT NULL ORDER BY updated_at")
        edited = 0
        for card in cards:
            if self._closed:
                return
            approval = self.store.approval(card["approval_id"])
            text, markup = self._render(card, approval=approval)
            digest = self._hash(text, markup)
            if digest == card["rendered_hash"]:
                continue
            if edited >= BATCH_SIZE:
                return
            edited += 1
            try:
                result = run_say([self.say, "--edit", str(card["telegram_message_id"]),
                                  "--markup", json.dumps(markup), text], self.timeout)
            except (OSError, subprocess.SubprocessError) as exc:
                self._update(card["token"], error=_text(str(exc), 400))
                continue
            if result.returncode == 0 and result.stdout.strip() == "edited":
                fields = {"rendered_hash": digest, "error": None}
                if approval["state"] != "pending":
                    fields.update(state="resolved", outcome=json.dumps(approval.get("decision") or approval["state"]))
                self._update(card["token"], **fields)
            else:
                self._update(card["token"], error=_text(result.stdout or result.stderr or "edit failed", 400))
