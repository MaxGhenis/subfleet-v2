"""The daemon's conversation service (C-24 to C-30; design §4, §5, §11).

It owns `conversations.sqlite3`, answers the conversation ops, dispatches each
conversation's next message as a turn job through the daemon's own submit
path, runs one `TurnRunner` per live turn attempt, and settles each message
from its turn's outcome. `daemon.py` calls it through a handful of seams:
`owns`/`respond` for ops, `tick` from the control loop, `launch` and
`TurnAdapter` for turn attempts, `stop` when a turn must be stopped.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .. import protocol
from ..adapters.base import AdapterError
from ..sessions import handoff as session_handoff
from ..sessions import registry, transcripts
from . import attachments as attachment_store
from . import codex_brief
from .classify import TurnAdapter, read_turn
from .launch import TURN_MANIFEST_KEY, claude_launch, codex_launch, lane_email, spec_from_manifest
from .peers import APP_EXECUTABLES, judge, peer_pid
from .runner import TurnRunner
from .store import (
    PROVIDERS, ConversationError, ConversationStore, _decode_message, canonical_uuid, validate_settings, widens,
    utcnow,
)
from .turn import (
    APPROVAL_NEEDED, CANCELLED, COMPLETE, DELIVERY_UNKNOWN, FAILED, INTERRUPTED, QUEUED, RUNNING,
    STARTING, TERMINAL_STATES, WAITING,
)

CAPABILITIES = ("conversations.v1", "events.v1", "approvals.v1", "attachments.v1", "catalog.v1", "watch.v1",
                "handoff.v1")
LIMITS = {"message_bytes": 1_048_576, "attachment_bytes": 20 * 1024 * 1024, "attachments_per_message": 8,
          "events_page_bytes": 262_144, "events_wait_s": 50, "relay_frame_bytes": 64 * 1024 * 1024}
OPS = frozenset(protocol.CONVERSATION_OPS)
POLL_OPS = frozenset({"conversation.events", "conversation.watch"})
# Ops that copy files or read a native transcript run on the bounded file pool (C-25.3).
FILE_OPS = frozenset({"attachment.add", "conversation.history", "conversation.handoff"})
PERSON_ONLY = frozenset({"approval.get", "approval.respond", "message.resolve", "conversation.unblock"})
MAX_WAIT_S = 50.0

# A failure that provably never delivered the message: the next turn job may
# carry the same message again (review IR-1, IR-23). Everything else settles it.
NOT_DELIVERED = frozenset({"stopped-before-send", "identity", "guard-refused", "settings-unsupported",
                           "effort-unsupported", "provider-init-failed", "thread-failed", "thread-mismatch",
                           "external-writer", "fast-unavailable", "model-mismatch-before-send"})
READMIT = frozenset({"external-writer", "fast-unavailable", "provider-init-failed", "guard-refused"})
MAX_READMITS = 3
CONTINUATION_TEXT = ("Continue from where you left off; the previous turn stopped at a usage limit "
                     "on another account.")
CODEX_WRITABLE_FLAG = "codex-writable-verified.json"
# The dispatcher's claim on a queued message while it creates the message's turn job
# (C-24.7, IR-2, IR-28): a claimed message is `waiting` with this reason and no job yet.
CLAIMED = "dispatching"
# A message whose turn submit was deferred is offered again after this long.
DEFER_S = 5.0
# A handoff moves the person's pending messages, withdraws Subfleet's failover
# continuations (they would resume the source's work beside the handoff), and leaves an
# unblock note in the source, where it still guards the source's next turn (IR-28).
HANDOFF_MOVES = ("person",)
HANDOFF_KEEPS = ("unblock-note",)
# The source's `blocked_by` from just before a handoff cancels a waiting message's
# job until its commit: `handoff:<request id>` (C-30.3, D-18).
HANDOFF_FENCE = "handoff:"


class ConversationService:
    def __init__(self, daemon):
        self.daemon = daemon
        self.root: Path = daemon.root
        self.store = ConversationStore(self.root)
        self.polls = concurrent.futures.ThreadPoolExecutor(8, thread_name_prefix="subfleet-poll")
        self.files = concurrent.futures.ThreadPoolExecutor(2, thread_name_prefix="subfleet-files")
        self.runners: dict[str, TurnRunner] = {}
        self._lock = threading.RLock()
        self._handing_off: set[str] = set()          # source conversations mid-handoff (IR-28)
        self._deferred: dict[str, float] = {}         # message id -> monotonic time of next submit
        self._poll_slots: dict[tuple, threading.Event] = {}
        self.log = daemon.log

    def close(self) -> None:
        for runner in list(self.runners.values()):
            runner.stop()
        self.polls.shutdown(wait=False, cancel_futures=True)
        self.files.shutdown(wait=False, cancel_futures=True)
        self.store.close()

    # --- the socket seam -------------------------------------------------------

    @staticmethod
    def owns(op: str) -> bool:
        return op in OPS

    def pool_for(self, op: str):
        if op in POLL_OPS:
            return self.polls
        if op in FILE_OPS:
            return self.files
        return self.daemon.requests

    def respond(self, conn, write_lock, req: protocol.Request, peer: int | None) -> None:
        try:
            response = protocol.ok(req.id, self.handle(req.op, req.args, peer))
        except ConversationError as exc:
            response = protocol.fail(req.id, exc.code, f"{exc.reason}: {exc}", exc.fix)
        except (protocol.ProtocolError, AdapterError) as exc:
            response = protocol.fail(req.id, exc.code, str(exc), exc.fix)
        except (ValueError, TypeError, KeyError) as exc:
            response = protocol.fail(req.id, 2, f"invalid arguments: {exc}")
        except Exception as exc:
            self.log.error("conversation op %s failed: %s: %s", req.op, type(exc).__name__, exc)
            response = protocol.fail(req.id, 1, "operation failed; inspect daemon status")
        try:
            with write_lock:
                conn.sendall(protocol.encode(response))
        except OSError:
            pass

    def handle(self, op: str, args: dict, peer: int | None) -> dict:
        if not isinstance(args, dict):
            raise ConversationError("bad-args", "args must be an object")
        handler = getattr(self, "op_" + op.replace(".", "_"))
        return handler(args, peer)

    def _app_executables(self) -> tuple[str, ...]:
        """C-25.6, D-21: the installed app. A development daemon (any state root but
        ~/.subfleet) also accepts the development build named when it started."""
        dev = os.environ.get("SUBFLEET_DEV_APP_EXECUTABLE")
        if dev and self.root.resolve() != (Path.home() / ".subfleet").resolve():
            return (*APP_EXECUTABLES, dev)
        return APP_EXECUTABLES

    def _person(self, peer: int | None, what: str):
        """C-25.6: refuse agents Subfleet launched; accept the app or a terminal."""
        verdict = judge(peer, root=str(self.root), app_executables=self._app_executables())
        if not verdict.person:
            raise ConversationError("person-only", f"{what} is a person's decision: {verdict.reason}", code=7,
                                    fix="answer it in the Subfleet app")
        return verdict

    # --- ops: discovery --------------------------------------------------------

    def op_capabilities(self, args, peer) -> dict:
        from .. import __version__
        return {"protocol": protocol.PROTOCOL_VERSION, "daemon_version": __version__,
                "conversation_schema": 1, "capabilities": list(CAPABILITIES), "limits": LIMITS,
                "codex_writable": self._codex_writable()}

    def op_models_list(self, args, peer) -> dict:
        """The models this fleet routes, each with the value a conversation stores
        and what the providers' own catalogs last said about it (design D-19)."""
        provider = args.get("provider")
        policy = self.daemon.policy
        observed = self._catalog_cache()
        models = []
        for short, entry in policy["models"].items():
            if provider and entry["provider"] != provider:
                continue
            seen = observed.get(entry["provider"], {}).get(entry["id"], {})
            # `default` is whatever one account's settings pick; a conversation names a model.
            values = [v for v in seen.get("values") or [] if v != "default"]
            models.append({"short": short, "id": entry["id"], "provider": entry["provider"],
                           "value": values[0] if values else entry["id"], "values": values or [entry["id"]],
                           "efforts": seen.get("efforts"), "default_effort": entry.get("effort"),
                           "fast": {"supported": seen.get("fast"),
                                    "billing": "usage credits" if entry["provider"] == "claude" else "plan limits"},
                           "image_input": seen.get("image_input"), "observed_at": seen.get("observed_at")})
        return {"models": models, "source": "policy, and each provider's catalog as a turn last reported it"}

    def _catalog_cache(self) -> dict:
        try:
            return json.loads((self.root / "conversations" / "models.json").read_text())
        except (OSError, ValueError):
            return {}

    def _on_catalog(self, provider: str, lane_id: str | None, catalog: list) -> None:
        """Merge one turn's provider catalog into `conversations/models.json`."""
        from ..guardian import atomic_publish
        with self._lock:
            data = self._catalog_cache()
            models: dict[str, dict] = {}
            for entry in catalog:
                model = models.setdefault(entry["model"], {"values": [], "efforts": entry.get("efforts"),
                                                           "fast": entry.get("fast"),
                                                           "image_input": entry.get("image_input")})
                if entry["value"] not in model["values"]:
                    model["values"].append(entry["value"])
                if entry.get("context_1m") is False and entry["value"] != "default":
                    # A value without the 1M context goes first: it is the model as named.
                    model["values"].remove(entry["value"])
                    model["values"].insert(0, entry["value"])
            now = utcnow()
            section = data.setdefault(provider, {})
            for model_id, model in models.items():
                lanes = {**(section.get(model_id) or {}).get("lanes", {}), **({lane_id: now} if lane_id else {})}
                section[model_id] = {**model, "observed_at": now, "lanes": lanes}
            path = self.root / "conversations" / "models.json"
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_publish(path, (json.dumps(data, sort_keys=True, indent=1) + "\n").encode())

    # --- ops: conversations ----------------------------------------------------

    def op_conversation_list(self, args, peer) -> dict:
        conversations = [self._view(c) for c in self.store.list_conversations(
            provider=args.get("provider"), limit=int(args.get("limit") or 200))]
        out = {"conversations": conversations}
        if args.get("include_catalog", True):
            from .catalog import read_catalog
            bound = {(c["provider"], c["native_session_id"]) for c in conversations if c["native_session_id"]}
            out["catalog"] = read_catalog(self.root, query=args.get("query"), exclude=bound,
                                          limit=int(args.get("limit") or 200), before=args.get("before"),
                                          include_archived=bool(args.get("include_archived")))
        return out

    def _view(self, conversation: dict) -> dict:
        last = self.store.one("SELECT message_id,state,state_reason,updated_at FROM messages WHERE conversation_id=? "
                              "ORDER BY seq DESC LIMIT 1", (conversation["conversation_id"],))
        pending = self.store.one("SELECT COUNT(*) n FROM approvals WHERE conversation_id=? AND state='pending'",
                                 (conversation["conversation_id"],))["n"]
        return {**{k: conversation[k] for k in ("conversation_id", "provider", "native_session_id", "title",
                                                "workspace", "workspace_kind", "allow_main", "lane_id", "settings",
                                                "origin", "handoff_from", "blocked_by", "created_at", "updated_at")},
                "last_message": last, "pending_approvals": pending,
                "active": bool(last and last["state"] not in TERMINAL_STATES and last["state"] != QUEUED)}

    def op_conversation_open(self, args, peer) -> dict:
        if args.get("conversation_id"):
            conversation = self.store.conversation(args["conversation_id"])
        else:
            native = args.get("native") or {}
            conversation = self._open_native(native)
        cid = conversation["conversation_id"]
        cursor = self.store.one("SELECT COALESCE(MAX(seq),0) s FROM events WHERE conversation_id=?", (cid,))["s"]
        return {"conversation": self._view(conversation), "messages": [self._receipt(m) for m in self.store.messages(cid)],
                "events_cursor": cursor, "pending_approvals": [self._approval_view(a) for a in
                                                               self.store.approvals(conversation_id=cid)]}

    def _open_native(self, native: dict) -> dict:
        from .catalog import native_session
        provider, session_id = native.get("provider"), native.get("session_id")
        if provider not in ("claude", "codex") or not isinstance(session_id, str) or not session_id:
            raise ConversationError("bad-native", "native needs a provider and a session_id")
        existing = self.store.by_native(provider, session_id)
        if existing:
            return existing
        found = native_session(provider, session_id, home=native.get("home"), root=self.root,
                               lanes=self.daemon.store.lane_rows())
        if found is None:
            raise ConversationError("unknown-session", f"no {provider} session {session_id} was found")
        if not found.get("continuable"):
            raise ConversationError("not-continuable", found.get("continue_blocker") or "this session cannot continue here",
                                    code=7, fix="use a handoff")
        settings = {"model": found["model_value"], "effort": None, "fast": False,
                    "permission": found["permission"], "auto_continue": True}
        conversation, _ = self.store.create_conversation(
            provider=provider, workspace=found["cwd"], workspace_kind="in-place", settings=settings, origin="native",
            native_session_id=session_id, title=found.get("title"), lane_id=found.get("lane_id"))
        return conversation

    def op_conversation_create(self, args, peer) -> dict:
        provider = args.get("provider")
        request_id = args.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ConversationError("bad-request-id", "request_id must be 1 to 128 characters")
        settings = validate_settings(provider, args.get("settings"))
        allow_main = bool(args.get("allow_main"))
        if allow_main or settings["permission"] in ("accept-edits", "bypass"):
            self._person(peer, "starting a conversation on main or above Ask")
            if settings["permission"] in ("accept-edits", "bypass") and args.get("confirm_widen") is not True:
                raise ConversationError("confirm-widen", "a policy above Ask needs confirm_widen: true")
        self._check_codex_policy(provider, settings)
        workspace = os.path.realpath(os.path.expanduser(str(args.get("workspace") or "")))
        if not os.path.isdir(workspace):
            raise ConversationError("bad-workspace", "workspace must be an existing directory")
        kind = args.get("workspace_kind") or "in-place"
        if kind not in ("in-place", "worktree"):
            raise ConversationError("bad-workspace", "workspace_kind is in-place or worktree")
        self._check_workspace(provider, workspace, settings)
        conversation, created = self.store.create_conversation(
            provider=provider, workspace=workspace, workspace_kind=kind, settings=settings, origin="new",
            title=args.get("title"), allow_main=allow_main, request_id=request_id)
        if created and kind == "worktree":
            path = self._cut_worktree(workspace, conversation["conversation_id"])
            conversation = self.store.update_conversation(conversation["conversation_id"], **{})
            self.store.query("UPDATE conversations SET workspace=? WHERE conversation_id=?",
                             (path, conversation["conversation_id"]))
            conversation = self.store.conversation(conversation["conversation_id"])
        return {"conversation": self._view(conversation), "created": created}

    def _cut_worktree(self, repo: str, cid: str) -> str:
        import subprocess
        top = subprocess.run(["git", "-C", repo, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=20)
        if top.returncode:
            raise ConversationError("not-a-repository", "a worktree conversation needs a git repository")
        target = self.root / "worktrees" / f"conversation-{cid}"
        branch = f"subfleet/{cid}"
        made = subprocess.run(["git", "-C", top.stdout.strip(), "worktree", "add", "-b", branch, str(target)],
                              capture_output=True, text=True, timeout=60)
        if made.returncode:
            raise ConversationError("worktree-failed", made.stderr.strip()[-300:] or "git worktree add failed", code=1)
        return str(target)

    def op_conversation_settings(self, args, peer) -> dict:
        conversation = self.store.conversation(args["conversation_id"])
        after = validate_settings(conversation["provider"], {**conversation["settings"], **(args.get("settings") or {})})
        fields: dict[str, Any] = {"settings": after}
        if widens(conversation["settings"], after):
            self._person(peer, "widening a conversation's permissions")
            if args.get("confirm_widen") is not True:
                raise ConversationError("confirm-widen", "a wider policy needs confirm_widen: true")
        if "allow_main" in args and bool(args["allow_main"]) != conversation["allow_main"]:
            if args["allow_main"]:
                self._person(peer, "working on main")
            fields["allow_main"] = bool(args["allow_main"])
        self._check_codex_policy(conversation["provider"], after)
        return {"conversation": self._view(self.store.update_conversation(conversation["conversation_id"], **fields))}

    def op_conversation_unblock(self, args, peer) -> dict:
        verdict = self._person(peer, "unblocking an unfinished turn")
        conversation = self.store.conversation(args["conversation_id"])
        if args.get("confirm") is not True or args.get("choice") not in ("continue", "leave"):
            raise ConversationError("confirm", "unblock needs choice continue|leave and confirm: true")
        if conversation["blocked_by"] not in ("unfinished-turn", "delivery-unknown"):
            raise ConversationError("not-blocked", "the conversation is not blocked by an unfinished turn")
        if conversation["blocked_by"] == "delivery-unknown":
            raise ConversationError("resolve-first", "resolve the delivery-unknown message first",
                                    fix="message.resolve")
        self._record_note(conversation, args["choice"], verdict)
        return {"conversation": self._view(self.store.update_conversation(conversation["conversation_id"],
                                                                          blocked_by=None))}

    def _record_note(self, conversation: dict, choice: str, verdict) -> None:
        """D-13: 'leave' sends a one-line note first so the next turn does not resume
        the stopped work; 'continue' lets Claude continue it."""
        if choice != "leave":
            return
        last = self.store.one("SELECT message_id FROM messages WHERE conversation_id=? AND origin='person' "
                              "ORDER BY seq DESC LIMIT 1", (conversation["conversation_id"],))
        text = ("[Subfleet] The previous turn was stopped by the person and must not be resumed. "
                "Wait for the next message.")
        self.store.submit_message(conversation_id=conversation["conversation_id"], message_id=str(uuid.uuid4()),
                                  after_message_id=last["message_id"] if last else None, text=text, attachments=[],
                                  settings=conversation["settings"], origin="unblock-note")

    def op_conversation_history(self, args, peer) -> dict:
        from .history import page
        conversation = self.store.conversation(args["conversation_id"])
        return page(conversation, root=self.root, before=args.get("before"), limit=int(args.get("limit") or 50),
                    lanes=self.daemon.store.lane_rows())

    # --- ops: events -----------------------------------------------------------

    def _slot(self, peer: int | None, kind: str) -> threading.Event:
        """C-29.9: one poll of each kind per client; a new one supersedes the old."""
        key = (peer, kind)
        with self._lock:
            old = self._poll_slots.get(key)
            if old is not None:
                old.set()
            flag = threading.Event()
            self._poll_slots[key] = flag
        return flag

    def op_conversation_events(self, args, peer) -> dict:
        cid = args["conversation_id"]
        self.store.conversation(cid)
        after = int(args.get("after") or 0)
        wait_s = max(0.0, min(float(args.get("wait_s") or 0), MAX_WAIT_S))
        superseded = self._slot(peer, "events")
        page = self.store.events_after(cid, after, limit=int(args.get("limit") or 500))
        if not page["events"] and not page["reset"] and wait_s:
            def ready():
                return superseded.is_set() or bool(self.store.one(
                    "SELECT 1 FROM events WHERE conversation_id=? AND seq>?", (cid, after)))
            self.store.wait(ready, wait_s)
            if superseded.is_set():
                return {"events": [], "next": after, "reset": False, "superseded": True}
            page = self.store.events_after(cid, after, limit=int(args.get("limit") or 500))
        return page

    def op_conversation_watch(self, args, peer) -> dict:
        after = int(args.get("after") or 0)
        wait_s = max(0.0, min(float(args.get("wait_s") or 0), MAX_WAIT_S))
        superseded = self._slot(peer, "watch")
        result = self.store.changes_after(after)
        if not result["changes"] and wait_s:
            self.store.wait(lambda: superseded.is_set() or bool(self.store.one("SELECT 1 FROM changes WHERE seq>?", (after,))),
                            wait_s)
            if superseded.is_set():
                return {"changes": [], "next": after, "superseded": True}
            result = self.store.changes_after(after)
        return result

    # --- ops: messages ---------------------------------------------------------

    def op_message_submit(self, args, peer) -> dict:
        conversation = self.store.conversation(args["conversation_id"])
        if conversation.get("archived_at"):
            raise ConversationError("archived", "the conversation is archived")
        settings = validate_settings(conversation["provider"], args.get("settings") or conversation["settings"])
        if widens(conversation["settings"], settings):
            raise ConversationError("settings-mismatch", "a message cannot widen the conversation's permissions",
                                    fix="change it with conversation.settings and confirm_widen: true")
        self._check_codex_policy(conversation["provider"], settings)
        attachments = list(args.get("attachments") or [])
        if conversation["provider"] == "claude":
            size = len(str(args.get("text") or "").encode()) + sum(
                (self.store.attachment(a) or {}).get("bytes", 0) * 4 // 3 + 64 for a in attachments)
            if size > LIMITS["relay_frame_bytes"] - 65_536:
                raise ConversationError("message-too-large", "text and images together are too large for one message")
        message_id = canonical_uuid(args["message_id"])
        tomb = self.store.one("SELECT * FROM messages WHERE message_id=? AND state='cancelled' "
                              "AND state_reason='withdrawn-before-receipt'", (message_id,))
        if tomb:
            return self._receipt(self.store.message(message_id), created=False)
        message, created = self.store.submit_message(
            conversation_id=conversation["conversation_id"], message_id=message_id,
            after_message_id=args.get("after_message_id"), text=str(args.get("text") or ""),
            attachments=attachments, settings=settings)
        if created:
            self.daemon._notify()
        return self._receipt(message, created=created)

    def op_message_status(self, args, peer) -> dict:
        out = []
        for mid in list(args.get("message_ids") or [])[:200]:
            try:
                out.append(self._receipt(self.store.message(canonical_uuid(mid))))
            except ConversationError:
                out.append({"message_id": mid, "state": "unknown"})
        return {"messages": out}

    def op_message_cancel(self, args, peer) -> dict:
        """IR-2, IR-7: withdraw only before the provider could have seen it."""
        message_id = canonical_uuid(args["message_id"])
        try:
            message = self.store.message(message_id)
        except ConversationError:
            return self._tombstone(message_id, args.get("conversation_id"))
        if message["state"] == QUEUED and not self._turn_job(message):
            if self.store.set_state(message_id, CANCELLED, reason="withdrawn", expect=(QUEUED,)):
                return self._receipt(self.store.message(message_id))
        if message["state"] in (QUEUED, WAITING):
            job = self._turn_job(message)
            if job and self._cancel_job_without_attempt(job["job_id"]):
                self.store.set_state(message_id, CANCELLED, reason="withdrawn", expect=(QUEUED, WAITING, STARTING))
                return self._receipt(self.store.message(message_id))
            if job is None and self.store.message(message_id)["state"] == WAITING:
                # Claimed by the dispatcher, whose job for it is being created (C-24.7).
                raise ConversationError("dispatching", "the message is being handed to its turn job",
                                        fix="send the cancel again in a moment")
        raise ConversationError("too-late", "the provider may already have this message", fix="use turn.interrupt")

    def _tombstone(self, message_id: str, conversation_id: str | None) -> dict:
        if not conversation_id:
            raise ConversationError("unknown-message", "cancel of an unknown message needs its conversation_id")
        conversation = self.store.conversation(conversation_id)
        last = self.store.one("SELECT message_id FROM messages WHERE conversation_id=? AND origin='person' "
                              "ORDER BY seq DESC LIMIT 1", (conversation_id,))
        message, _ = self.store.submit_message(conversation_id=conversation_id, message_id=message_id,
                                               after_message_id=last["message_id"] if last else None,
                                               text="(withdrawn before it was received)", attachments=[],
                                               settings=conversation["settings"], origin="tombstone")
        self.store.set_state(message_id, CANCELLED, reason="withdrawn-before-receipt", expect=(QUEUED,))
        return self._receipt(self.store.message(message_id))

    def _cancel_job_without_attempt(self, job_id: str, *, by: dict | None = None) -> bool:
        """The job store decides: cancelled only while no attempt row exists (IR-2).

        `by` is written on the cancel's own audit event, in the same transaction,
        so a handoff that never committed can find the jobs it cancelled and put
        their messages back (C-30.3, D-18)."""
        daemon = self.daemon
        with daemon.store.transaction("job.cancel_requested", job_id=job_id, data=by) as tx:
            if tx.execute("SELECT 1 FROM attempts WHERE job_id=?", (job_id,)).fetchone():
                return False
            changed = tx.execute("UPDATE jobs SET cancel_requested_at=COALESCE(cancel_requested_at,?), state='cancelled', "
                                 "rc=130, finished_at=?, wait_reason=NULL, next_check_at=NULL "
                                 "WHERE job_id=? AND state IN ('queued','waiting')", (utcnow(), utcnow(), job_id)).rowcount
            tx.execute("DELETE FROM leases WHERE holder=?", (job_id,))
        daemon._notify()
        return bool(changed)

    def op_turn_interrupt(self, args, peer) -> dict:
        message_id = canonical_uuid(args["message_id"])
        message = self.store.message(message_id)
        if message["state"] not in (STARTING, RUNNING, APPROVAL_NEEDED, WAITING):
            raise ConversationError("not-running", f"the message is {message['state']}")
        self.store.update_message(message_id, stop_requested_at=utcnow())
        runner = self._runner_for_message(message_id)
        if runner is not None:
            runner.interrupt("stopped")
        elif message["state"] == WAITING:
            job = self._turn_job(message)
            if job and self._cancel_job_without_attempt(job["job_id"]):
                self.store.set_state(message_id, CANCELLED, reason="withdrawn", expect=(WAITING,))
        return self._receipt(self.store.message(message_id))

    def op_message_resolve(self, args, peer) -> dict:
        verdict = self._person(peer, "resolving an ambiguous delivery")
        message_id = canonical_uuid(args["message_id"])
        if args.get("confirm") is not True or args.get("resolution") not in ("delivered", "not-delivered"):
            raise ConversationError("confirm", "resolve needs resolution delivered|not-delivered and confirm: true")
        message = self.store.message(message_id)
        if message["state"] != DELIVERY_UNKNOWN:
            raise ConversationError("not-ambiguous", f"the message is {message['state']}")
        record = {"resolution": args["resolution"], "by": {"pid": verdict.pid, "as": verdict.reason},
                  "at": utcnow()}
        self.store.set_state(message_id, FAILED, reason=f"resolved-{args['resolution']}",
                             expect=(DELIVERY_UNKNOWN,), resolution=record)
        conversation = self.store.conversation(message["conversation_id"])
        if conversation["blocked_by"] == "delivery-unknown":
            self.store.update_conversation(conversation["conversation_id"], blocked_by=None)
        return self._receipt(self.store.message(message_id))

    def _receipt(self, message: dict, *, created: bool | None = None) -> dict:
        out = {k: message.get(k) for k in ("message_id", "conversation_id", "seq", "origin", "continues", "state",
                                           "state_reason", "settings", "served", "turn_ref", "updated_at")}
        out["stop_requested"] = bool(message.get("stop_requested_at"))
        if created is not None:
            out["created"] = created
        return out

    # --- ops: approvals --------------------------------------------------------

    def _approval_view(self, approval: dict) -> dict:
        return {k: approval[k] for k in ("approval_id", "message_id", "conversation_id", "kind", "display",
                                         "options", "created_at", "state")}

    def op_approval_list(self, args, peer) -> dict:
        return {"approvals": [self._approval_view(a) for a in self.store.approvals(
            conversation_id=args.get("conversation_id"))]}

    def op_approval_get(self, args, peer) -> dict:
        self._person(peer, "reading an approval")
        from .redact import mask_approval
        approval = self.store.approval(args["approval_id"])
        request = json.loads(Path(approval["request_path"]).read_text())
        masked, spans = mask_approval(request) if not args.get("reveal") else (request, [])
        return {"approval": self._approval_view(approval), "request": masked, "masked": spans,
                "request_sha256": approval["request_sha256"], "nonce": approval["nonce"]}

    def op_approval_respond(self, args, peer) -> dict:
        verdict = self._person(peer, "answering an approval")
        approval = self.store.approval(args["approval_id"])
        if approval["state"] != "pending":
            if approval["decision"] and approval["decision"].get("decision") == args.get("decision"):
                return {"approval": self._approval_view(approval), "duplicate": True}
            raise ConversationError("not-pending", f"the approval is {approval['state']}")
        if args.get("nonce") != approval["nonce"] or args.get("request_sha256") != approval["request_sha256"]:
            raise ConversationError("stale-approval", "the approval shown is not the one pending")
        decision = args.get("decision")
        if decision not in approval["options"]:
            raise ConversationError("bad-decision", f"{decision!r} is not offered here")
        runner = self.runners.get(approval["attempt_id"])
        if runner is None or runner.driver.outcome is not None:
            raise ConversationError("turn-ended", "the turn that asked has ended")
        record = {"decision": decision, "message": args.get("message"), "answers": args.get("answers"),
                  "by": {"pid": verdict.pid, "as": verdict.reason}, "at": utcnow()}
        if not self.store.answer_approval(approval["approval_id"], record):
            raise ConversationError("not-pending", "the approval was answered or withdrawn meanwhile")
        runner.respond(approval["provider_request_id"], decision, args.get("message"), args.get("answers"))
        return {"approval": self._approval_view(self.store.approval(approval["approval_id"]))}

    # --- ops: handoff (C-30.3, design D-18, review IR-28) ----------------------

    def op_conversation_handoff(self, args, peer) -> dict:
        """A labelled handoff: a new conversation whose first message is a scrubbed,
        bounded brief of the source's native history, recording where it came from
        (C-30.3, D-18). It is a new native session, never the source's. The source's
        pending messages move behind the brief in their order, each withdrawn from
        the source under the guard `message.cancel` uses (IR-28); a source with a
        live turn is refused. Idempotent by `request_id`: the same request returns
        the same conversation, a different request under that id is exit 2."""
        request_id = args.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ConversationError("bad-request-id", "request_id must be 1 to 128 characters")
        source_arg, target = args.get("from"), args.get("to")
        if not isinstance(source_arg, dict) or not isinstance(target, dict):
            raise ConversationError("bad-args", "a handoff needs `from` and `to` objects")
        provider = target.get("provider")
        if provider not in PROVIDERS:
            raise ConversationError("bad-provider", "to.provider must be claude or codex")
        settings = validate_settings(provider, target.get("settings"))
        title = target.get("title")
        if title is not None and (not isinstance(title, str) or len(title) > 200):
            raise ConversationError("bad-title", "to.title must be a string of at most 200 characters")
        allow_main = bool(target.get("allow_main"))
        digest = hashlib.sha256(json.dumps(
            {"from": source_arg, "provider": provider, "settings": settings, "workspace": target.get("workspace"),
             "title": title, "allow_main": allow_main}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        existing = self.store.by_request(request_id)
        if existing:
            return self._handoff_result(existing, digest=digest, created=False)
        if allow_main or settings["permission"] in ("accept-edits", "bypass"):
            # IR-21: a conversation above Ask, or on main, is a person's to start.
            self._person(peer, "handing off to a conversation on main or above Ask")
            if settings["permission"] in ("accept-edits", "bypass") and args.get("confirm_widen") is not True:
                raise ConversationError("confirm-widen", "a policy above Ask needs confirm_widen: true")
        self._check_codex_policy(provider, settings)
        conversation, source_provider, session_id, home = self._handoff_origin(source_arg)
        cid = conversation["conversation_id"] if conversation else None
        if cid:
            with self._lock:
                if cid in self._handing_off:
                    raise ConversationError("handoff-running", "another handoff of this conversation is running",
                                            fix="send the same request again when it ends")
                self._handing_off.add(cid)          # the dispatcher leaves its messages alone meanwhile
        fence = None
        try:
            if conversation is not None:
                # A handoff of this source that a failure or a restart cut short
                # left its fence: put back what it withdrew before anything else.
                conversation = self._lift_stale_fence(conversation)
            marker = _handoff_marker(request_id)
            # A live turn is the reason to refuse whatever else is true of the source.
            plan = self._handoff_plan(conversation) if conversation else []
            if not session_id:
                raise ConversationError("no-history", "the source conversation has no native session yet",
                                        fix="cancel its messages and start a new conversation instead")
            source = self._handoff_history(conversation, source_provider, session_id, home)
            workspace = self._handoff_workspace(target.get("workspace"), source["cwd"])
            self._check_workspace(provider, workspace, settings)
            brief = self._handoff_brief(source, workspace)
            ids = {"brief": _handoff_id(request_id, "brief")}
            moves = [{"message_id": _handoff_id(request_id, step["message"]["message_id"]),
                      "text": self.store.message_text(step["message"]),
                      "attachments": step["message"]["attachments"], "from": step["message"]["message_id"]}
                     for step in plan if step["message"]["origin"] in HANDOFF_MOVES]
            record = {"provider": source["provider"], "native_session_id": source["native_session_id"],
                      "transcript": str(source["transcript"]),
                      "brief_sha256": hashlib.sha256(brief.text.encode("utf-8")).hexdigest(),
                      "conversation_id": cid, "lane_id": source["lane_id"], "brief_message_id": ids["brief"],
                      "moved": [{"from": m["from"], "to": m["message_id"]} for m in moves],
                      "withdrawn": [step["message"]["message_id"] for step in plan],
                      "redactions": brief.redactions, "request_digest": digest, "at": utcnow()}
            # Everything that can fail before the commit is done by now: the texts
            # are read and published and the attachments checked (D-18).
            prepared = self.store.prepare_handoff(
                request_id=request_id, provider=provider, workspace=workspace, settings=settings,
                title=title or (conversation or {}).get("title"), allow_main=allow_main, handoff_from=record,
                brief={"message_id": ids["brief"], "text": brief.text}, moves=moves)
            if prepared.get("existing"):
                created_conversation, created = prepared["existing"], False
            else:
                try:
                    jobs = [step["job_id"] for step in plan if step["job_id"]]
                    if jobs:
                        # The job store's cancel below cannot be undone by the
                        # conversation store's commit, so the source is fenced
                        # durably first: the dispatcher skips a blocked
                        # conversation, and so nothing behind the waiting message
                        # can run there before it (C-30.3, IR-28).
                        fence = f"{HANDOFF_FENCE}{request_id}"
                        if not self.store.fence(cid, fence):
                            fence = None
                            raise ConversationError("source-changed", "the source was blocked during the handoff",
                                                    fix="send the same handoff request again")
                    # IR-2's guard for a message whose turn job exists: its job is
                    # cancelled only while it has no attempt. C-24.5 allows at most
                    # one such message.
                    for job_id in jobs:
                        if not self._cancel_job_without_attempt(job_id, by=marker):
                            raise self._not_withdrawn(job_id)
                    created_conversation, created = self.store.commit_handoff(
                        prepared, withdrawals=[{"message_id": step["message"]["message_id"], "expect": step["expect"]}
                                               for step in plan],
                        fence=(cid, fence) if fence else None)
                except BaseException:
                    self.store.discard_handoff(prepared)
                    if fence:
                        self._lift_fence_quietly(cid, fence)
                    raise
        finally:
            if cid:
                with self._lock:
                    self._handing_off.discard(cid)
        if created:
            self.daemon._notify()
        return self._handoff_result(created_conversation, digest=digest, created=created)

    def _handoff_result(self, conversation: dict, *, digest: str, created: bool) -> dict:
        record = conversation.get("handoff_from") or {}
        if conversation.get("origin") != "handoff" or record.get("request_digest") != digest:
            raise ConversationError("request-id-conflict", "request_id already names a different conversation",
                                    fix="use a new request_id for a new handoff")
        return {"conversation": self._view(conversation), "created": created,
                "brief": self._receipt(self.store.message(record["brief_message_id"])),
                "moved": [self._receipt(self.store.message(m["to"])) for m in record.get("moved", [])],
                "withdrawn": record.get("withdrawn", []), "handoff_from": record}

    def _not_withdrawn(self, job_id: str) -> ConversationError:
        """Why the job store's guard refused a handoff's cancel (IR-2)."""
        job = self.daemon.store.get_job(job_id) or {}
        if job.get("state") in ("succeeded", "failed", "cancelled", "lost"):
            return ConversationError("source-changed", "the source's waiting message was settled during the handoff",
                                     fix="send the same handoff request again")
        return ConversationError("live-turn", "the source's waiting message started its turn",
                                 fix="stop it with turn.interrupt, or wait for it to end")

    @staticmethod
    def _handoff_workspace(requested: Any, source_cwd: str | None) -> str:
        """D-18: the source's workspace unless the request names another."""
        workspace = os.path.realpath(os.path.expanduser(str(requested or source_cwd or "")))
        if not os.path.isdir(workspace):
            raise ConversationError("bad-workspace", "the handoff's workspace must be an existing directory",
                                    fix="pass to.workspace")
        return workspace

    def _lift_fence(self, cid: str, fence: str) -> list[str]:
        """Undo a handoff of `cid` that never committed (C-30.3, D-18): each source
        message whose turn job that handoff cancelled (its audit event carries the
        handoff's marker) is queued again in its place, and the fence is lifted,
        in one conversation-store transaction. The caller holds `_handing_off`."""
        marker = _handoff_marker(fence[len(HANDOFF_FENCE):])
        restores = [{"message_id": m["message_id"], "job_id": m["job_id"]} for m in self.store.query(
            "SELECT message_id, job_id, state_reason FROM messages WHERE conversation_id=? AND job_id IS NOT NULL "
            "AND state IN (?,?) ORDER BY seq", (cid, WAITING, CANCELLED))
            if not str(m["state_reason"] or "").startswith("handed-off:") and self._cancelled_by(m["job_id"], marker)]
        restored = self.store.restore_after_handoff(cid, fence, restores)
        if restored:
            self.log.warning("handoff %s did not complete; put back %d message(s) in %s",
                             fence[len(HANDOFF_FENCE):], len(restored), cid)
            self.daemon._notify()
        return restored

    def _lift_fence_quietly(self, cid: str, fence: str) -> None:
        """On a handoff's failure path: a lift that fails is left to the tick."""
        try:
            self._lift_fence(cid, fence)
        except Exception as exc:
            self.log.error("lifting handoff fence %s of %s failed: %s: %s", fence, cid, type(exc).__name__, exc)

    def _lift_stale_fence(self, conversation: dict) -> dict:
        """A fence nobody holds is one a failed or interrupted handoff left: lift
        it. The caller holds `_handing_off` for this conversation."""
        blocked = conversation.get("blocked_by") or ""
        if not blocked.startswith(HANDOFF_FENCE):
            return conversation
        self._lift_fence(conversation["conversation_id"], blocked)
        return self.store.conversation(conversation["conversation_id"])

    def _lift_stale_fences(self) -> None:
        """Tick: after a restart, or when a failure path could not, lift the fences
        of handoffs that are not running, so their sources are never left blocked."""
        rows = self.store.query("SELECT conversation_id, blocked_by FROM conversations WHERE blocked_by LIKE ?",
                                (f"{HANDOFF_FENCE}%",))
        for row in rows:
            cid = row["conversation_id"]
            with self._lock:
                if cid in self._handing_off:
                    continue                        # its handoff is running; it lifts its own fence
                self._handing_off.add(cid)
            try:
                self._lift_fence(cid, row["blocked_by"])
            finally:
                with self._lock:
                    self._handing_off.discard(cid)

    def _handoff_origin(self, source: dict) -> tuple[dict | None, str, str | None, str | None]:
        """(conversation, provider, native id, Codex home) the handoff comes from. A
        native session some conversation already holds hands off as that
        conversation, so its pending messages and its live turn count."""
        if source.get("conversation_id"):
            conversation = self.store.conversation(str(source["conversation_id"]))
            return conversation, conversation["provider"], conversation["native_session_id"], None
        native = source.get("native")
        if not isinstance(native, dict):
            raise ConversationError("bad-args", "from needs a conversation_id or a native session")
        provider, session_id = native.get("provider"), native.get("session_id")
        if provider not in PROVIDERS or not isinstance(session_id, str) or not session_id:
            raise ConversationError("bad-native", "from.native needs a provider and a session_id")
        try:
            session_id = (session_handoff.canonical_session_id(session_id) if provider == "claude"
                          else codex_brief.canonical_thread_id(session_id))
        except session_handoff.HandoffError as exc:
            raise ConversationError("bad-native", str(exc)) from exc
        return self.store.by_native(provider, session_id), provider, session_id, native.get("home")

    def _handoff_history(self, conversation: dict | None, provider: str, session_id: str,
                         home: str | None) -> dict:
        """The native transcript the brief is read from (bounded file pool, C-25.3)."""
        try:
            if provider == "claude":
                return self._claude_source(conversation, session_id)
            return self._codex_source(conversation, session_id, home)
        except session_handoff.HandoffError as exc:
            raise ConversationError("handoff-source", str(exc), code=exc.code, fix=exc.fix) from exc

    def _claude_source(self, conversation: dict | None, session_id: str) -> dict:
        path = transcripts.transcript_path(session_id)
        if path is None:
            raise ConversationError("unknown-session", f"no transcript for Claude session {session_id}")
        if conversation is None and registry.is_lane_run(session_id, lane_ids=self.daemon._lane_session_ids(),
                                                         transcript=path):
            # C-23.31: a headless lane run is one brief and one answer, not a conversation.
            raise ConversationError("lane-run", f"{session_id} is a headless lane run (claude -p), not a session",
                                    code=7, fix="`subfleet runs show <job>` for what that lane produced")
        cwd = (conversation["workspace"] if conversation else
               session_handoff.latest_metadata(path, max_bytes=session_handoff.FULL_SCAN_BYTES)[1])
        return {"conversation": conversation, "provider": "claude", "native_session_id": session_id,
                "transcript": path, "source_cwd": cwd, "cwd": cwd, "lane_id": None}

    def _codex_source(self, conversation: dict | None, thread_id: str, home: str | None) -> dict:
        lanes = [r for r in self.daemon.store.lane_rows() if r.get("provider") == "codex" and r.get("home")]
        homes: list[tuple[Path, str | None]]
        if conversation and conversation["lane_id"]:
            homes = [(Path(r["home"]), r["lane_id"]) for r in lanes if r["lane_id"] == conversation["lane_id"]]
        else:
            allowed = [(Path(r["home"]).expanduser().resolve(), r["lane_id"]) for r in lanes]
            allowed.append(((Path.home() / ".codex").resolve(), None))
            if home:
                wanted = Path(str(home)).expanduser().resolve()
                homes = [pair for pair in allowed if pair[0] == wanted]
                if not homes:
                    raise ConversationError("bad-native", "from.native.home must be an enrolled Codex lane home "
                                            "or ~/.codex")
            else:
                homes = allowed
        path = lane_id = None
        for base, lane in homes:
            path = codex_brief.find_rollout(thread_id, [base])
            if path is not None:
                lane_id = lane
                break
        if path is None:
            raise ConversationError("unknown-session", f"no rollout for Codex thread {thread_id}")
        if conversation is None and codex_brief.headless_run(path):
            raise ConversationError("lane-run", f"{thread_id} is a Codex exec or subagent run, not a conversation",
                                    code=7, fix="`subfleet runs show <job>` for what that run produced")
        cwd = conversation["workspace"] if conversation else codex_brief.session_meta(path).get("cwd")
        return {"conversation": conversation, "provider": "codex", "native_session_id": thread_id,
                "transcript": path, "source_cwd": cwd, "cwd": cwd,
                "lane_id": (conversation or {}).get("lane_id") or lane_id}

    def _handoff_plan(self, conversation: dict) -> list[dict]:
        """IR-28: what leaves the source, or a refusal while the source has a live turn.

        A message still `queued` with no job leaves under the conversation store's
        own guard (it must still be `queued` when the handoff commits; the
        dispatcher claims a message before it creates its job). A `waiting`
        message whose job has no attempt leaves under the job store's guard, as
        `message.cancel` withdraws it. Anything a provider may already have is a
        live turn. A handoff that never committed has been undone before this
        runs (`_lift_stale_fence`), so every cancelled job here was a person's.
        """
        cid = conversation["conversation_id"]
        plan: list[dict] = []
        withdrawable: set[str] = set()
        rows = self.store.query("SELECT * FROM messages WHERE conversation_id=? AND state NOT IN (?,?,?) ORDER BY seq",
                                (cid, COMPLETE, FAILED, INTERRUPTED))
        for message in map(_decode_message, rows):
            state = message["state"]
            if message["origin"] in HANDOFF_KEEPS and state == QUEUED and self._turn_job(message) is None:
                continue
            if state == CANCELLED:
                continue
            if state not in (QUEUED, WAITING):
                raise ConversationError("live-turn", f"the source has a live turn (a message is {state})",
                                        fix="stop it with turn.interrupt or wait for it to end; a delivery-unknown "
                                            "message needs message.resolve first")
            job = self._turn_job(message)
            if job is None:
                if state == WAITING:
                    raise ConversationError("live-turn", "the source's next message is being dispatched",
                                            fix="send the same request again in a moment")
                plan.append({"message": message, "expect": (QUEUED,), "job_id": None})
                continue
            if self.daemon.store.one("SELECT 1 FROM attempts WHERE job_id=?", (job["job_id"],)):
                raise ConversationError("live-turn", "the source's waiting message has started its turn",
                                        fix="stop it with turn.interrupt, or wait for it to end")
            if job["state"] == "cancelled":
                continue                            # already withdrawn by message.cancel; not pending
            withdrawable.add(job["job_id"])
            # The tick may settle the message `cancelled` once its job is; that is still ours to move.
            plan.append({"message": message, "expect": (QUEUED, WAITING, CANCELLED), "job_id": job["job_id"]})
        for job in self.daemon.store.query(
                "SELECT job_id FROM jobs WHERE kind='turn' AND name=? AND state NOT IN "
                "('succeeded','failed','cancelled','lost')", (f"turn-{cid}",)):
            if job["job_id"] not in withdrawable:
                raise ConversationError("live-turn", "a turn of the source is still running",
                                        fix="wait for it to end, or stop it with turn.interrupt")
        if self.daemon.store.one("SELECT 1 FROM leases WHERE lease_key=?", (f"conversation:{cid}",)) or \
                self.daemon.store.one("SELECT 1 FROM attempts a JOIN jobs j USING(job_id) WHERE j.kind='turn' "
                                      "AND j.name=? AND a.state='quarantined'", (f"turn-{cid}",)):
            raise ConversationError("live-turn", "a turn of the source still holds the conversation",
                                    fix="wait for it to finish, or resolve its quarantine")
        return plan

    def _handoff_brief(self, source: dict, workspace: str):
        """C-23.14, C-23.36: the scrubbed, bounded brief. No git: the handler never
        waits on git (C-25.3), so the repository section says it was not collected."""
        caps = dict(self.daemon.policy.get("sessions", {}).get("handoff_caps") or {})
        build = session_handoff.build_brief if source["provider"] == "claude" else codex_brief.build_brief
        try:
            return build(source["native_session_id"], Path(source["transcript"]), Path(workspace),
                         source["source_cwd"], caps, repository=False)
        except session_handoff.HandoffError as exc:
            raise ConversationError("handoff-source", str(exc), code=exc.code, fix=exc.fix) from exc

    def _cancelled_by(self, job_id: str, marker: dict) -> bool:
        """Did the cancel recorded for this job carry `marker` (a handoff's own)?"""
        return bool(self.daemon.store.one(
            "SELECT 1 FROM events WHERE kind='job.cancel_requested' AND job_id=? "
            "AND json_extract(data_json,'$.by')=? AND json_extract(data_json,'$.request_id')=?",
            (job_id, marker["by"], marker["request_id"])))

    # --- ops: attachments, catalog ---------------------------------------------

    def op_attachment_add(self, args, peer) -> dict:
        return attachment_store.add(self.store, args.get("path"), args.get("sha256"))

    def op_catalog_refresh(self, args, peer) -> dict:
        from .catalog import request_refresh
        return request_refresh(self.root)

    # --- policy checks ---------------------------------------------------------

    def _codex_writable(self) -> bool:
        return (self.root / "conversations" / CODEX_WRITABLE_FLAG).is_file()

    def _check_codex_policy(self, provider: str, settings: dict) -> None:
        """C-26.11, IR-32: writable Codex turns wait for the live never-rules test."""
        if provider == "codex" and settings["permission"] != "read-only" and not self._codex_writable():
            raise ConversationError("codex-read-only", "Codex conversations are read-only until the never-rules "
                                    "hook is shown firing in a live app-server turn", code=7)

    def _check_workspace(self, provider: str, workspace: str, settings: dict) -> None:
        """C-26.10: a writable turn may not own the state root or a provider home."""
        writable = settings["permission"] == "accept-edits" or (provider == "codex" and settings["permission"] != "read-only")
        if not writable:
            return
        protected = [self.root, Path.home() / ".claude", Path.home() / ".codex"]
        protected += [Path(row["home"]) for row in self.daemon.store.lane_rows() if row.get("home")]
        real = Path(workspace).resolve()
        for path in protected:
            p = path.expanduser().resolve()
            if real == p or real in p.parents:
                raise ConversationError("protected-workspace", f"{workspace} contains {p}", code=7,
                                        fix="choose a narrower directory, or Ask or read-only")

    # --- the control loop ------------------------------------------------------

    def tick(self) -> None:
        """Called on the control loop's worker pool, never on a request thread."""
        try:
            self._lift_stale_fences()
            self._dispatch()
            self._adopt_runners()
            self._settle_unstarted()
        except Exception as exc:
            self.log.error("conversation tick failed: %s: %s", type(exc).__name__, exc)

    def _turn_job(self, message: dict) -> dict | None:
        """The main store decides which job carries a message (IR-1)."""
        row = self.daemon.store.one("SELECT * FROM jobs WHERE request_id=? AND kind='turn'",
                                    (f"turn:{message['message_id']}:{message['turn_seq']}",))
        return dict(row) if row else None

    def _dispatch(self) -> None:
        now = time.monotonic()
        self._deferred = {mid: at for mid, at in self._deferred.items() if at > now}
        for message in self.store.next_dispatchable() + self._readmittable():
            mid = message["message_id"]
            if message["conversation_id"] in self._handing_off or self._deferred.get(mid, 0) > now:
                continue            # IR-28: a handoff is taking this conversation's pending messages
            conversation = self.store.conversation(message["conversation_id"])
            job = self._turn_job(message)
            if job is None and not self._previous_released(conversation, message):
                continue
            if job is None:
                claimed = False
                if message["state"] == QUEUED:
                    # C-24.7, IR-2, IR-28: claim the message in the conversation store
                    # before its job exists. A withdrawal of a queued message
                    # (message.cancel, conversation.handoff) and this claim are one
                    # store's transactions, so exactly one of them wins: a withdrawn
                    # message never gets a job, and a claimed one is withdrawn only
                    # through its job's no-attempt guard.
                    if not self.store.set_state(mid, WAITING, reason=CLAIMED, expect=(QUEUED,)):
                        continue
                    claimed = True
                try:
                    job = self._submit_turn(conversation, message)
                except ConversationError as exc:
                    self.store.set_state(mid, FAILED, reason=exc.reason, expect=(QUEUED, WAITING))
                    continue
                except (protocol.ProtocolError, AdapterError, OSError, ValueError) as exc:
                    # Refused before any provider saw it: the message waits, it is not failed.
                    # A claimed one goes back to `queued`, so it can still be withdrawn.
                    if claimed and self._turn_job(message) is None:
                        self.store.set_state(mid, QUEUED, expect=(WAITING,))
                    else:
                        self.store.update_message(mid)
                    self._deferred[mid] = time.monotonic() + DEFER_S
                    self.log.warning("turn submit for %s deferred: %s", mid, exc)
                    continue
                self._deferred.pop(mid, None)
            if job and message["state"] in (QUEUED, WAITING):
                reason = message.get("state_reason")
                self.store.set_state(mid, WAITING, reason=None if reason == CLAIMED else reason,
                                     expect=(QUEUED, WAITING), job_id=job["job_id"])

    def _readmittable(self) -> list[dict]:
        """Waiting messages with no job bound: a re-admission, or a claim a crash
        interrupted before its job was bound (design §4's repair: an existing job is
        bound, a missing one is submitted)."""
        rows = self.store.query("SELECT * FROM messages WHERE state='waiting' AND job_id IS NULL "
                                "AND (state_reason LIKE 'readmit:%' OR state_reason=?)", (CLAIMED,))
        return [_decode_message(row) for row in rows]

    def _previous_released(self, conversation: dict, message: dict) -> bool:
        """C-24.5: the previous turn job is terminal and holds no lease."""
        rows = self.daemon.store.query(
            "SELECT job_id, state FROM jobs WHERE kind='turn' AND name=? AND job_id<>COALESCE(?, '') "
            "ORDER BY created_at DESC LIMIT 3", (f"turn-{conversation['conversation_id']}", message.get("job_id")))
        for row in rows:
            if row["state"] not in ("succeeded", "failed", "cancelled", "lost"):
                return False
            if self.daemon.store.one("SELECT 1 FROM leases WHERE holder=?", (row["job_id"],)):
                return False
        quarantined = self.daemon.store.one(
            "SELECT 1 FROM attempts a JOIN jobs j USING(job_id) WHERE j.kind='turn' AND j.name=? AND a.state='quarantined'",
            (f"turn-{conversation['conversation_id']}",))
        if quarantined:
            if conversation["blocked_by"] != "quarantined-turn":
                self.store.update_conversation(conversation["conversation_id"], blocked_by="quarantined-turn")
            return False
        return True

    def _submit_turn(self, conversation: dict, message: dict) -> dict:
        daemon = self.daemon
        provider = conversation["provider"]
        settings = message["settings"]
        short = policy_model(daemon.policy, provider, settings["model"])
        images = []
        for sha in message["attachments"]:
            path, media = attachment_store.check(self.store, sha)
            images.append({"sha256": sha, "media_type": media, "path": path})
        native = conversation["native_session_id"]
        new_session = None
        if provider == "claude" and not native:
            # Minted once per message, so a re-admitted turn resumes the same new session.
            new_session = str(uuid.uuid5(uuid.NAMESPACE_URL, f"subfleet:{message['message_id']}"))
        affinity = None
        if provider == "claude":
            prev = self.store.one("SELECT served_json FROM messages WHERE conversation_id=? AND served_json IS NOT NULL "
                                  "ORDER BY seq DESC LIMIT 1", (conversation["conversation_id"],))
            affinity = (json.loads(prev["served_json"]) or {}).get("lane_id") if prev else None
        turn = {"conversation_id": conversation["conversation_id"], "message_id": message["message_id"],
                "provider": provider, "text": self.store.message_text(message), "settings": settings,
                "native_session_id": native, "new_session_id": new_session, "images": images,
                "cwd": conversation["workspace"], "allow_main": conversation["allow_main"],
                "affinity_lane": affinity, "digest": message["digest"]}
        exclusions = []
        if message.get("continues"):
            original = self.store.message(message["continues"])
            if original.get("served") and original["served"].get("lane_id"):
                exclusions.append(original["served"]["lane_id"])
        args = protocol.SubmitArgs(
            request_id=f"turn:{message['message_id']}:{message['turn_seq']}", kind="turn",
            workdir=conversation["workspace"], prompt_path=message["text_path"],
            sandbox="read-only" if settings["permission"] == "read-only" else "workspace-write",
            pinned_model=short, pinned_lane=conversation["lane_id"] if provider == "codex" else None,
            name=f"turn-{conversation['conversation_id']}", exclusions=exclusions, in_place=True,
            independent=True, no_preamble=True, max_attempts=1, allow_tmp=True)
        result = daemon.submit(args, turn=turn)
        return dict(daemon.store.one("SELECT * FROM jobs WHERE job_id=?", (result["job_id"],)))

    def _adopt_runners(self) -> None:
        rows = self.daemon.store.query(
            "SELECT a.*, j.kind FROM attempts a JOIN jobs j USING(job_id) WHERE j.kind='turn' "
            "AND a.state IN ('starting','running','finalizing')")
        for attempt in rows:
            aid = attempt["attempt_id"]
            if aid in self.runners:
                continue
            adir = self.root / "jobs" / attempt["job_id"] / f"a{attempt['seq']}"
            start = _read_json(adir / "start.json")
            if not start or not start.get("control_socket"):
                continue
            manifest = _read_json(self.root / "jobs" / attempt["job_id"] / "manifest.json") or {}
            turn = manifest.get(TURN_MANIFEST_KEY)
            launch = _read_json(adir / "launch.json") or {}
            if not turn:
                continue
            notes = launch.get("notes") or {}
            lane = self.daemon.store.get_lane(attempt["lane_id"])
            spec = spec_from_manifest(turn, lane_email=lane_email(lane) if lane else None,
                                      guard_hash=notes.get("guard_hash"), model_ref=notes.get("model_id"))
            runner = TurnRunner(store=self.store, attempt=dict(attempt), spec=spec,
                                conversation_id=turn["conversation_id"], attempt_dir=adir,
                                control_socket=start["control_socket"], on_outcome=self._on_outcome,
                                on_contain=self._on_contain, log=self.log,
                                approval_wait_s=float(self.daemon.policy.get("conversations", {}).get("approval_wait_s", 3600)),
                                on_catalog=self._on_catalog)
            self.runners[aid] = runner
            self.store.set_state(turn["message_id"], STARTING, expect=(WAITING,), job_id=attempt["job_id"])
            runner.start()

    def _settle_unstarted(self) -> None:
        """A waiting message whose job ended with no provider start was never delivered."""
        for message in self.store.query("SELECT * FROM messages WHERE state IN ('waiting','starting') AND job_id IS NOT NULL"):
            job = self.daemon.store.one("SELECT * FROM jobs WHERE job_id=?", (message["job_id"],))
            if not job or job["state"] not in ("succeeded", "failed", "cancelled", "lost"):
                continue
            attempts = self.daemon.store.query("SELECT attempt_id, state, outcome_detail FROM attempts WHERE job_id=?",
                                               (job["job_id"],))
            if any(a["attempt_id"] in self.runners for a in attempts):
                continue
            started = any((self.root / "jobs" / job["job_id"] / f"a{i + 1}" / "stdin.jsonl").exists()
                          for i in range(len(attempts)))
            if started:
                continue            # the runner's outcome settles it
            reason = "cancelled" if job["state"] == "cancelled" else "not-delivered"
            state = CANCELLED if job["state"] == "cancelled" else FAILED
            detail = attempts[-1]["outcome_detail"] if attempts else job.get("rc")
            self.store.set_state(message["message_id"], state, reason=f"{reason}: {detail}"[:200],
                                 expect=(WAITING, STARTING))

    # --- outcomes --------------------------------------------------------------

    def _runner_for_message(self, message_id: str) -> TurnRunner | None:
        return next((r for r in self.runners.values() if r.message_id == message_id and not r.finished.is_set()), None)

    def _on_contain(self, attempt_id: str) -> None:
        """D-13 step 4: escalation reached containment; the daemon's kill path runs."""
        runner = self.runners.get(attempt_id)
        if runner is not None:
            runner.contained = True
        daemon = self.daemon
        job_id = attempt_id.rsplit("/", 1)[0]
        with daemon.store.transaction("job.cancel_requested", job_id=job_id, attempt_id=attempt_id) as tx:
            tx.execute("UPDATE jobs SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE job_id=?",
                       (utcnow(), job_id))
            tx.execute("UPDATE attempts SET killed_by=COALESCE(killed_by,?) WHERE attempt_id=?",
                       ((runner.stop_reason if runner else None) or "interrupt", attempt_id))
        daemon._notify()

    def stop(self, attempt: dict, job: dict, reason: str) -> bool:
        """Called by the daemon where it would kill a turn attempt (wall limit, a
        kill request). True while the D-13 escalation owns the stop."""
        runner = self.runners.get(attempt["attempt_id"])
        if runner is None or runner.contained or runner.finished.is_set():
            return False
        if runner.stop_at is None:
            runner.interrupt(reason)
        return True

    def _on_outcome(self, runner: TurnRunner) -> None:
        """Settle a message from its turn (D-12, D-14, C-24.8, C-26.7)."""
        turn = read_turn(runner.adir) or {}
        message = self.store.message(runner.message_id)
        conversation = self.store.conversation(runner.conversation_id)
        state, reason = turn.get("state"), turn.get("reason")
        served = {**(message.get("served") or {}), **(turn.get("served") or {}),
                  "lane_id": runner.attempt.get("lane_id"), "model": turn.get("served_model")}
        native = turn.get("native_session_id")
        if native and not conversation["native_session_id"]:
            self.store.update_conversation(conversation["conversation_id"], native_session_id=native,
                                           **({"lane_id": runner.attempt.get("lane_id")}
                                              if conversation["provider"] == "codex" else {}))
        live = (STARTING, RUNNING, APPROVAL_NEEDED, WAITING)
        self.store.withdraw_approvals(attempt_id=runner.attempt_id)
        if state == COMPLETE:
            self.store.set_state(message["message_id"], COMPLETE, reason="stop-too-late" if turn.get("stop_too_late") else None,
                                 expect=live, served=served, turn_ref=turn.get("turn_id") or message.get("turn_ref"))
        elif state == INTERRUPTED and turn.get("accepted"):
            self.store.set_state(message["message_id"], INTERRUPTED, reason=turn.get("stop_reason") or reason,
                                 expect=live, served=served)
        elif reason in NOT_DELIVERED and not turn.get("user_frame_written"):
            readmits = message["turn_seq"]
            if reason in READMIT and readmits < MAX_READMITS:
                self.store.set_state(message["message_id"], WAITING, reason=f"readmit:{reason}", expect=live,
                                     turn_seq=readmits + 1, job_id=None)
            else:
                self.store.set_state(message["message_id"], FAILED, reason=f"not-delivered: {reason}",
                                     expect=live, served=served)
        elif reason == "limited":
            self.store.set_state(message["message_id"], FAILED, reason="limited", expect=live, served=served)
            self._continue_elsewhere(conversation, message)
        elif turn.get("user_frame_written") or turn.get("accepted"):
            # Delivered, and no terminal event: reconcile, and block a Claude
            # conversation until the person decides (C-24.8, IR-5).
            if state == INTERRUPTED or turn.get("stop_reason"):
                self.store.set_state(message["message_id"], INTERRUPTED, reason=turn.get("stop_reason") or "stopped",
                                     expect=live, served=served)
            else:
                self.store.set_state(message["message_id"], FAILED, reason=reason or "ended-without-result",
                                     expect=live, served=served)
            if conversation["provider"] == "claude" and not turn.get("state") == COMPLETE and reason in (
                    "ended-without-result", "stopped", None):
                self.store.update_conversation(conversation["conversation_id"], blocked_by="unfinished-turn")
        else:
            self.store.set_state(message["message_id"], DELIVERY_UNKNOWN, reason=reason or "no-evidence",
                                 expect=live, served=served)
            self.store.update_conversation(conversation["conversation_id"], blocked_by="delivery-unknown")
        self.daemon._notify()

    def _continue_elsewhere(self, conversation: dict, message: dict) -> None:
        """D-6, C-26.7: a Claude conversation continues on another account with a
        labelled continuation; the original is never sent again."""
        if conversation["provider"] != "claude" or not conversation["settings"].get("auto_continue", True):
            return
        chain, current = 0, message
        while current.get("continues"):
            chain += 1
            current = self.store.message(current["continues"])
        if chain >= 2:
            return
        last = self.store.one("SELECT message_id FROM messages WHERE conversation_id=? AND origin='person' "
                              "ORDER BY seq DESC LIMIT 1", (conversation["conversation_id"],))
        self.store.submit_message(conversation_id=conversation["conversation_id"], message_id=str(uuid.uuid4()),
                                  after_message_id=last["message_id"] if last else None, text=CONTINUATION_TEXT,
                                  attachments=[], settings=message["settings"], origin="failover",
                                  continues=message["message_id"])

    # --- the daemon's launch and finalize seams --------------------------------

    def launch(self, job: dict, attempt: dict, lane, credential_env: dict, adir: Path, model_id: str,
               guard_result=None):
        manifest = _read_json(self.root / "jobs" / job["job_id"] / "manifest.json") or {}
        turn = manifest.get(TURN_MANIFEST_KEY)
        if not turn:
            raise AdapterError("turn job has no turn manifest", fix="the dispatcher creates turn jobs")
        if lane.provider == "claude":
            return claude_launch(turn, attempt_id=attempt["attempt_id"], attempt_dir=adir, lane=lane,
                                 credential_env=credential_env, model_id=model_id)
        if guard_result is None or not guard_result.override:
            raise AdapterError("a Codex turn needs the verified never-rules guard", code=7)
        launch = codex_launch(turn, attempt_id=attempt["attempt_id"], attempt_dir=adir, lane=lane,
                              credential_env=credential_env, model_id=model_id,
                              executable=guard_result.executable or "codex", override=guard_result.override,
                              unified_exec_off=os.environ.get("SUBFLEET_CODEX_UNIFIED_EXEC") == "off")
        launch.notes["guard_hash"] = guard_result.hooks_hash
        return launch

    def release_socket(self, attempt_id: str) -> None:
        """A guardian stopped by containment never removes its relay socket;
        finalization does (the guardian's own exit path already has)."""
        from ..relay import socket_path
        path = socket_path(self.root, attempt_id)
        try:
            if stat.S_ISSOCK(os.lstat(path).st_mode):
                os.unlink(path)
        except OSError:
            pass

    @staticmethod
    def adapter(provider: str) -> TurnAdapter:
        return TurnAdapter(provider)


def policy_model(policy: dict, provider: str, value: str) -> str:
    """A conversation's model value to the policy model admission routes (D-19)."""
    base = value[:-4] if value.endswith("[1m]") else value
    models = policy["models"]
    if base in models and models[base]["provider"] == provider:
        return base
    for short, entry in models.items():
        if entry["provider"] == provider and entry["id"] == base:
            return short
    raise ConversationError("unknown-model", f"{value!r} is not a {provider} model this fleet routes",
                            fix="pick a model from models.list")


def _handoff_marker(request_id: str) -> dict:
    """What a handoff writes on the audit event of each job cancel it makes (D-18)."""
    return {"by": "conversation.handoff", "request_id": request_id}


def _handoff_id(request_id: str, part: str) -> str:
    """A message id a handoff mints, the same on every retry of one request (C-24.2)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"subfleet:handoff:{request_id}:{part}"))


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        return None
