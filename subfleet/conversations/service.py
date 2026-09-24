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
from ..salvage import SalvageError
from . import attachments as attachment_store
from . import diff as turn_diff
from .classify import TurnAdapter, read_turn
from .launch import TURN_MANIFEST_KEY, claude_launch, codex_launch, lane_email, spec_from_manifest
from .peers import APP_EXECUTABLES, judge, peer_pid
from .runner import TurnRunner
from .store import (
    ConversationError, ConversationStore, canonical_uuid, validate_settings, widens, utcnow,
)
from .turn import (
    APPROVAL_NEEDED, CANCELLED, COMPLETE, DELIVERY_UNKNOWN, FAILED, INTERRUPTED, QUEUED, RUNNING,
    STARTING, TERMINAL_STATES, WAITING,
)

CAPABILITIES = ("conversations.v1", "events.v1", "approvals.v1", "attachments.v1", "catalog.v1", "watch.v1",
                "diff.v1")
LIMITS = {"message_bytes": 1_048_576, "attachment_bytes": 20 * 1024 * 1024, "attachments_per_message": 8,
          "events_page_bytes": 262_144, "events_wait_s": 50, "relay_frame_bytes": 64 * 1024 * 1024,
          "diff_bytes": turn_diff.DIFF_BYTES, "diff_files": turn_diff.DIFF_FILES}
OPS = frozenset(protocol.CONVERSATION_OPS)
POLL_OPS = frozenset({"conversation.events", "conversation.watch"})
# C-25.3: file copies, transcript reads and git (the diffs) run here, never on a request thread.
FILE_OPS = frozenset({"attachment.add", "conversation.history", "turn.diff", "conversation.diff"})
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


class ConversationService:
    def __init__(self, daemon):
        self.daemon = daemon
        self.root: Path = daemon.root
        self.store = ConversationStore(self.root)
        self.polls = concurrent.futures.ThreadPoolExecutor(8, thread_name_prefix="subfleet-poll")
        self.files = concurrent.futures.ThreadPoolExecutor(2, thread_name_prefix="subfleet-files")
        self.runners: dict[str, TurnRunner] = {}
        self._lock = threading.RLock()
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

    def _cancel_job_without_attempt(self, job_id: str) -> bool:
        """The job store decides: cancelled only while no attempt row exists (IR-2)."""
        daemon = self.daemon
        with daemon.store.transaction("job.cancel_requested", job_id=job_id) as tx:
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

    # --- ops: changes (C-26.13, design D-25) ------------------------------------

    def _git_cap(self) -> float:
        return float(self.daemon.policy["caps"]["workspace_git_timeout_s"])

    def op_turn_diff(self, args, peer) -> dict:
        """What one turn changed: its start snapshot to its end snapshot, or to the
        working tree now while the turn has no end yet (`to.live`)."""
        message = self.store.message(canonical_uuid(args.get("message_id")))
        path = turn_diff.pathspec(args.get("path"))
        head = {"message_id": message["message_id"], "conversation_id": message["conversation_id"]}
        row = self.store.turn_trees(message["message_id"])
        if row is None:
            return {**head, **_unavailable("no-turn", "the message has not started a turn")}
        if not row["writable"]:
            return {**head, **_unavailable("read-only-turn", "a read-only turn does not write")}
        if not row["start_tree"]:
            return {**head, **_unavailable("no-snapshot", "the workspace was not a git checkout with a commit "
                                                          "when the turn started")}
        if row["error"]:
            return {**head, **_unavailable("snapshot-failed", row["error"])}
        if row["ended_at"] and not row["end_tree"]:
            return {**head, **_unavailable("snapshot-failed", "the turn ended without an end snapshot")}
        start = {"tree": row["start_tree"], "head": row["head_before"], "message_id": message["message_id"],
                 "at": row["started_at"]}
        if row["end_tree"]:
            end = {"tree": row["end_tree"], "head": row["head_after"], "live": False, "at": row["ended_at"]}
            return {**head, **self._compare(row["workspace"], start, end, path)}
        return {**head, **self._compare(row["workspace"], start, None, path)}

    def op_conversation_diff(self, args, peer) -> dict:
        """What the conversation changed: its first writable turn's start snapshot
        to the working tree now."""
        conversation = self.store.conversation(args.get("conversation_id"))
        path = turn_diff.pathspec(args.get("path"))
        head = {"conversation_id": conversation["conversation_id"]}
        row = self.store.first_trees(conversation["conversation_id"])
        if row is None:
            return {**head, **_unavailable("no-snapshot", "no writable turn of this conversation has started "
                                                          "in a git checkout with a commit")}
        start = {"tree": row["start_tree"], "head": row["head_before"], "message_id": row["message_id"],
                 "at": row["started_at"]}
        return {**head, **self._compare(conversation["workspace"], start, None, path)}

    def _compare(self, workspace: str, start: dict, end: dict | None, path: str | None) -> dict:
        """Diff two snapshots; with no `end`, snapshot the working tree now (C-6.8's
        temporary index: no ref, the real index and the files untouched)."""
        cap = self._git_cap()
        try:
            if end is None:
                now = turn_diff.snapshot(workspace, timeout_s=cap)
                if now is None:
                    return _unavailable("no-snapshot", "the workspace is no longer a git checkout with a commit")
                end = {"tree": now[1], "head": now[0], "live": True, "at": utcnow()}
            result = turn_diff.build(workspace, start["tree"], end["tree"], path=path, timeout_s=cap)
            root = turn_diff.toplevel(workspace, timeout_s=cap)
        except turn_diff.Unavailable as exc:
            return _unavailable(exc.reason, str(exc))
        except SalvageError as exc:
            raise ConversationError("diff-failed", str(exc), code=1,
                                    fix="try again" if exc.transient else "check the workspace's repository")
        return {"available": True, "root": root, "path": path, "from": start, "to": end, **result}

    def record_trees(self, turn: dict, attempt: dict, receipt: dict) -> None:
        """The daemon's finalization seam: a turn attempt's end (C-26.10, C-26.13)."""
        self.store.record_trees(
            attempt_id=attempt["attempt_id"], message_id=turn["message_id"], conversation_id=turn["conversation_id"],
            workspace=receipt.get("workspace") or turn["cwd"], writable=bool(receipt.get("writable")),
            started_at=attempt.get("reserved_at") or utcnow(), head_before=receipt.get("head_before"),
            start_tree=receipt.get("start_tree"), head_after=receipt.get("head_after"),
            end_tree=receipt.get("end_tree"), error=receipt.get("error"), ended=True)

    def _record_start(self, turn: dict, attempt: dict) -> None:
        """A turn attempt's start, as admission recorded it (C-6.8's snapshot is the
        attempt's `baseline_tree` for a writable job; a read-only one keeps HEAD's tree,
        which is not a snapshot of the working tree and is not recorded here)."""
        writable = attempt.get("job_sandbox") == "workspace-write"
        evidence = json.loads(attempt.get("evidence_json") or "{}")
        self.store.record_trees(
            attempt_id=attempt["attempt_id"], message_id=turn["message_id"], conversation_id=turn["conversation_id"],
            workspace=turn["cwd"], writable=writable, started_at=attempt.get("reserved_at") or utcnow(),
            head_before=evidence.get("baseline_commit"),
            start_tree=attempt.get("baseline_tree") if writable else None)

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
        for message in self.store.next_dispatchable() + self._readmittable():
            conversation = self.store.conversation(message["conversation_id"])
            job = self._turn_job(message)
            if job is None and not self._previous_released(conversation, message):
                continue
            if job is None:
                try:
                    job = self._submit_turn(conversation, message)
                except ConversationError as exc:
                    self.store.set_state(message["message_id"], FAILED, reason=exc.reason,
                                         expect=(QUEUED, WAITING))
                    continue
                except (protocol.ProtocolError, AdapterError, OSError, ValueError) as exc:
                    # Refused before any provider saw it: the message waits, it is not failed.
                    self.store.update_message(message["message_id"])
                    self.log.warning("turn submit for %s deferred: %s", message["message_id"], exc)
                    continue
            if job and message["state"] in (QUEUED, WAITING):
                self.store.set_state(message["message_id"], WAITING, reason=message.get("state_reason"),
                                     expect=(QUEUED, WAITING), job_id=job["job_id"])

    def _readmittable(self) -> list[dict]:
        rows = self.store.query("SELECT * FROM messages WHERE state='waiting' AND state_reason LIKE 'readmit:%'")
        out = []
        from .store import _decode_message
        for row in rows:
            message = _decode_message(row)
            if not self._turn_job(message):
                out.append(message)
        return out

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
            "SELECT a.*, j.kind, j.sandbox AS job_sandbox FROM attempts a JOIN jobs j USING(job_id) WHERE j.kind='turn' "
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
            try:
                self._record_start(turn, dict(attempt))   # C-26.13: the turn's diff has a base
            except Exception as exc:                      # finalization records it again; a turn never waits on it
                self.log.warning("turn %s start snapshot not recorded: %s: %s", aid, type(exc).__name__, exc)

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


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        return None


def _unavailable(reason: str, detail: str) -> dict:
    """A diff with nothing to compare: the same shape, empty, and why (C-26.13)."""
    return {"available": False, "reason": reason, "detail": detail, "files": [], "files_truncated": False,
            "stats": {"files": 0, "additions": 0, "deletions": 0, "complete": True}, "diff": "",
            "truncated": False, "scrubbed": 0}
