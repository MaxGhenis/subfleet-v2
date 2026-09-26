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
import dataclasses
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import protocol
from ..adapters.base import AdapterError
from ..contracts import Exit
from ..policy import CONVERSATION_DEFAULTS
from ..relay import FRAME_MAX as RELAY_FRAME_MAX
from ..salvage import SalvageError
from . import attachments as attachment_store
from . import codex_turn, reconcile
from . import diff as turn_diff
from ..sessions import handoff as session_handoff
from ..sessions import registry, transcripts
from . import codex_brief
from .classify import TurnAdapter, read_turn
from .launch import TURN_MANIFEST_KEY, claude_launch, codex_launch, lane_email, spec_from_manifest
from .peers import APP_EXECUTABLES, judge, peer_pid
from .reconcile import MAX_READMITS, READMIT  # noqa: F401  (re-exported for callers)
from .runner import Clocks, TurnRunner
from .store import (
    LEGACY_OWNER, PROVIDERS, ConversationError, ConversationStore, _decode_message, canonical_native, canonical_uuid,
    validate_settings, widens,
    utcnow,
)
from .turn import (
    APPROVAL_NEEDED, CANCELLED, COMPLETE, DELIVERY_UNKNOWN, FAILED, INTERRUPTED, QUEUED, RUNNING,
    STARTING, TERMINAL_STATES, WAITING,
)

# `jobs.kind.v1`: `list` honours `kind` and `include_turns` and leaves turn jobs out by
# default; `status.json` rows carry `kind` (C-18.2, C-26.12, C-29.6).
#: C-25.1, C-25.2: the version of the conversation ops' argument and result shapes
#: (design §5). It is not the conversation store's schema (`store.SCHEMA_VERSION`,
#: C-26.14), and an added op is a capability, not a new version.
CONVERSATION_SCHEMA = 1
CAPABILITIES = ("conversations.v1", "events.v1", "approvals.v1", "attachments.v1", "catalog.v1", "watch.v1",
                protocol.JOBS_KIND_CAPABILITY, "diff.v1", "runs.v1",
                "handoff.v1")
# `relay_frame_bytes` is the relay's own cap (review IR-27), one definition in `relay.py`.
LIMITS = {"message_bytes": 1_048_576, "attachment_bytes": 20 * 1024 * 1024, "attachments_per_message": 8,
          "events_page_bytes": 262_144, "events_wait_s": 50, "relay_frame_bytes": RELAY_FRAME_MAX,
          "diff_bytes": turn_diff.DIFF_BYTES, "diff_files": turn_diff.DIFF_FILES}
OPS = frozenset(protocol.CONVERSATION_OPS)
POLL_OPS = frozenset({"conversation.events", "conversation.watch"})
# C-25.3: file copies, transcript reads and git (the diffs, and the worktree a
# worktree conversation's create cuts) run here, never on a request thread.
FILE_OPS = frozenset({"attachment.add", "conversation.history", "turn.diff", "conversation.diff",
                      "conversation.create", "conversation.handoff"})
PERSON_ONLY = frozenset({"approval.get", "approval.respond", "message.resolve", "conversation.unblock"})
MAX_WAIT_S = 50.0
RECEIPT_TEXT_CHARS = 20_000

# How a message settles, and when it may be carried again, is `reconcile.py`'s.
CONTINUATION_TEXT = ("Continue from where you left off; the previous turn stopped at a usage limit "
                     "on another account.")
CODEX_WRITABLE_FLAG = "codex-writable-verified.json"
# A turn submit refused before any provider saw the message waits and is tried
# again after 2, 4, 8, ... seconds, at most every 5 minutes (C-26.1).
DEFER_BASE_S = 2.0
#: How often a turn held by another Claude process looks again (C-26.3, D-17).
EXTERNAL_WRITER_RECHECK_S = 5.0
#: A Codex thread's other writer is seen only by starting a provider: every 30 s.
CODEX_WRITER_RECHECK_S = 30.0
DEFER_MAX_S = 300.0
# The handover locks (`ConversationService._handover`, C-24.7).
HANDOVER_STRIPES = 64
# A catalog run caps its own reading at 20 s (C-30.1); one still alive at three
# times that is wedged outside the cap (a directory walk, `ps`) and is stopped.
CATALOG_KILL_AFTER_S = 60.0
# How long close() waits for a catalog run to end after SIGTERM, and again after SIGKILL.
CATALOG_STOP_WAIT_S = 2.0
# How long close() waits, in all, for the turn runners to finish the iteration they are
# in. The conversation store refuses what a runner still going after it writes (C-25.3).
RUNNER_STOP_WAIT_S = 5.0
# Attempt states that have ended; `quarantined` has not (its processes may live).
ATTEMPT_ENDED = ("succeeded", "failed", "interrupted", "lost", "cancelled")
# The dispatcher's claim on a queued message while it creates the message's turn job
# (C-24.7, IR-2, IR-28): a claimed message is `waiting` with this reason and no job yet.
CLAIMED = "dispatching"
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
        self._poll_slots: dict[tuple, threading.Event] = {}
        # A person's cancel and stop of messages are taken one at a time (C-24.7).
        self._stops = threading.Lock()
        # C-24.7: a person's stop is recorded, and a runner hands its message frame
        # to the relay, under the message's lock, so whichever comes first is seen
        # by the other: a stop recorded first means the message is never written.
        # Striped by message id, so a relay slow to answer holds up few others.
        self._handovers = tuple(threading.Lock() for _ in range(HANDOVER_STRIPES))
        self.log = daemon.log
        self.clock = time.monotonic
        # message id -> (refusals in a row, monotonic time of the next try)
        self._deferred: dict[str, tuple[int, float]] = {}
        self._catalog_lock = threading.RLock()
        self._catalog_proc: subprocess.Popen | None = None
        self._catalog_started = 0.0            # monotonic time of the run this service last started
        self._catalog_killed = False
        self._catalog_last: float | None = None   # the last start or request; None: the first tick starts one
        self._catalog_fence: tuple[int, int] | None = None   # (read, write): catalog.Owner
        self._closed = False

    def close(self) -> None:
        self._stop_catalog()                    # from here `_closed`: no runner is adopted after
        with self._lock:
            runners = list(self.runners.values())
        for runner in runners:
            runner.stop()
        deadline = time.monotonic() + RUNNER_STOP_WAIT_S
        self.polls.shutdown(wait=False, cancel_futures=True)
        # File ops write into the state root: an attachment's copy, a worktree. One
        # still running when close() returned finished after its owner had removed
        # the root, and made it again (review of #47). The ones running finish here,
        # while the root and the store are still there; queued ones never run, and
        # the pool takes none after. Each is bounded (a capped file read, git under
        # its caps), as the ops the daemon's own pools wait for are.
        self.files.shutdown(wait=True, cancel_futures=True)
        # A turn runner writes into the state root too: the store, an approval's request,
        # `conversations/models.json`, `turn.json`. One still in its iteration when close()
        # returned made the removed root again. Each finishes that iteration here, within
        # a bound; the store refuses what one still going writes after it closes (`writing`).
        late = [runner.attempt_id for runner in runners if not runner.join(deadline - time.monotonic())]
        if late:
            self.log.warning("turn runners still going %g s after close(); the conversation store refuses "
                             "what they write now: %s", RUNNER_STOP_WAIT_S, ", ".join(late))
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
                "conversation_schema": CONVERSATION_SCHEMA, "capabilities": list(CAPABILITIES), "limits": LIMITS,
                "codex_writable": self._codex_writable()}

    def op_conversation_runs(self, args, peer) -> dict:
        """The detached jobs a conversation's turns dispatched (design §12): what
        its sub-agents are, where each runs and on which model. A Claude turn's
        tools carry its session id, which `subfleet run` records as the caller."""
        conversation = self.store.conversation(args["conversation_id"])
        sid = conversation.get("native_session_id")
        limit = max(1, min(int(args.get("limit") or 50), 200))
        if not sid:
            return {"runs": []}
        latest = "(SELECT {col} FROM attempts a WHERE a.job_id=j.job_id ORDER BY a.seq DESC LIMIT 1)"
        rows = self.daemon.store.query(
            "SELECT j.job_id, j.name, j.kind, j.state, j.task, j.tier, j.sandbox, j.wait_reason, j.created_at, "
            "j.started_at, j.finished_at, j.out_path, j.workdir, "
            f"{latest.format(col='lane_id')} AS lane_id, {latest.format(col='model_served')} AS model_served, "
            f"{latest.format(col='model_requested')} AS model_requested, "
            f"{latest.format(col='state')} AS attempt_state, "
            "(SELECT COUNT(*) FROM attempts a WHERE a.job_id=j.job_id) AS attempts "
            # A turn is the conversation itself and a revive continues its session:
            # neither is a sub-agent it dispatched.
            "FROM jobs j WHERE j.caller_session=? AND j.kind NOT IN ('turn','revive') "
            "ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?",
            (sid, limit))
        return {"runs": rows}

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
            with self.store.writing():          # open, and never making the state root (C-25.3)
                path = self.store.subdirectory("conversations") / "models.json"
                atomic_publish(path, (json.dumps(data, sort_keys=True, indent=1) + "\n").encode())

    # --- ops: conversations ----------------------------------------------------

    def config(self) -> dict:
        """The policy's `conversations` section over its defaults (C-26.9, C-30.1, C-25.4)."""
        return {**CONVERSATION_DEFAULTS, **(self.daemon.policy.get("conversations") or {})}

    def op_conversation_list(self, args, peer) -> dict:
        conversations = [self._view(c) for c in self.store.list_conversations(
            provider=args.get("provider"), limit=int(args.get("limit") or 200))]
        out = {"conversations": conversations}
        if args.get("include_catalog", True):
            from .catalog import read_catalog, refresh_running
            bound = {(c["provider"], c["native_session_id"]) for c in conversations if c["native_session_id"]}
            # D-23: the catalog is read as it is; a missing or old one is reported,
            # and the control loop's next run replaces it. Nothing here scans.
            catalog = read_catalog(self.root, query=args.get("query"), exclude=bound,
                                   limit=int(args.get("limit") or 200), before=args.get("before"),
                                   include_archived=bool(args.get("include_archived")),
                                   stale_after_s=self._catalog_stale_after_s())
            catalog["refreshing"] = bool(refresh_running(self.root))
            live = set(catalog.pop("live_elsewhere", ()))
            for view in conversations:
                view["live_elsewhere"] = (view["provider"] == "claude"
                                          and canonical_native(view["native_session_id"]) in live)
            out["catalog"] = catalog
        return out

    def _view(self, conversation: dict) -> dict:
        last = self.store.one("SELECT message_id,state,state_reason,updated_at FROM messages WHERE conversation_id=? "
                              "ORDER BY seq DESC LIMIT 1", (conversation["conversation_id"],))
        pending = self.store.one("SELECT COUNT(*) n FROM approvals WHERE conversation_id=? AND state='pending'",
                                 (conversation["conversation_id"],))["n"]
        return {**{k: conversation.get(k) for k in ("conversation_id", "provider", "native_session_id", "title",
                                                    "workspace", "workspace_kind", "worktree", "allow_main", "lane_id",
                                                    "settings", "origin", "handoff_from", "legacy_hold", "created_at",
                                                    "updated_at")},
                # C-30.4: the legacy import's hold is its own column, so no outcome
                # lifts it, but to a client it is a block like any other (the app
                # shows a conversation with `blocked_by` as needing a decision).
                "blocked_by": conversation.get("blocked_by") or (LEGACY_OWNER if conversation.get("legacy_hold") else None),
                "last_message": last, "pending_approvals": pending,
                "active": bool(last and last["state"] not in TERMINAL_STATES and last["state"] != QUEUED)}

    def _view_live(self, conversation: dict) -> dict:
        """`_view` plus `live_elsewhere`, from the last catalog run (D-23: no scan)."""
        view = self._view(conversation)
        view["live_elsewhere"] = False
        if view["provider"] == "claude" and view["native_session_id"]:
            from .catalog import read_catalog
            live = read_catalog(self.root, limit=1, stale_after_s=self._catalog_stale_after_s())["live_elsewhere"]
            view["live_elsewhere"] = canonical_native(view["native_session_id"]) in live
        return view

    def op_conversation_open(self, args, peer) -> dict:
        if args.get("conversation_id"):
            conversation = self.store.conversation(args["conversation_id"])
        else:
            native = args.get("native") or {}
            conversation = self._open_native(native)
        cid = conversation["conversation_id"]
        cursor = self.store.one("SELECT COALESCE(MAX(seq),0) s FROM events WHERE conversation_id=?", (cid,))["s"]
        return {"conversation": self._view_live(conversation), "messages": [self._receipt(m, text=True) for m in self.store.messages(cid)],
                "events_cursor": cursor, "pending_approvals": [self._approval_view(a) for a in
                                                               self.store.approvals(conversation_id=cid)]}

    def _open_native(self, native: dict) -> dict:
        from .catalog import native_session
        provider, session_id = native.get("provider"), native.get("session_id")
        if provider not in ("claude", "codex") or not isinstance(session_id, str) or not session_id:
            raise ConversationError("bad-native", "native needs a provider and a session_id")
        session_id = canonical_native(session_id)          # one spelling per session (review L1)
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
        existing = self.store.one("SELECT conversation_id FROM conversations WHERE request_id=?", (request_id,))
        if kind == "worktree" and existing is None and self._git_toplevel(workspace) is None:
            raise ConversationError("not-a-repository", "a worktree conversation needs a git repository")
        conversation, created = self.store.create_conversation(
            provider=provider, workspace=workspace, workspace_kind=kind, settings=settings, origin="new",
            title=args.get("title"), allow_main=allow_main, request_id=request_id)
        if conversation["workspace_kind"] == "worktree" and not conversation.get("worktree"):
            # A new conversation, or a repeat of a create whose cut failed or was
            # cut short by a crash: the same request id finishes the same worktree.
            conversation = self._cut_worktree(conversation)
        return {"conversation": self._view(conversation), "created": created}

    def _git_toplevel(self, directory: str) -> str | None:
        from ..salvage import git_toplevel
        try:
            return git_toplevel(directory, timeout_s=self._git_timeout_s())
        except SalvageError as exc:
            raise ConversationError("git-unavailable", f"could not inspect {directory}: {exc}", code=1,
                                    fix="try again") from exc

    def _git_timeout_s(self, key: str = "workspace_git_timeout_s") -> float:
        return float((self.daemon.policy.get("caps") or {}).get(key) or 60)

    def _cut_worktree(self, conversation: dict) -> dict:
        """D-16, D-25: a new worktree on its own branch, cut from the checkout the
        person picked, recorded on the conversation (path, branch, source, base).
        Idempotent: a worktree an interrupted earlier call already added on this
        conversation's branch is adopted; a directory that is not one is rebuilt."""
        cid = conversation["conversation_id"]
        source = conversation["workspace"]
        top = self._git_toplevel(source)
        if top is None:
            raise ConversationError("not-a-repository", "a worktree conversation needs a git repository")
        parent = self.store.subdirectory("worktrees")
        target = parent / f"conversation-{cid}"
        branch = f"subfleet/{cid}"
        timeout = self._git_timeout_s()

        def git(*argv: str, cwd: str | Path = top, cap: float = timeout) -> subprocess.CompletedProcess:
            try:
                return subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, text=True, timeout=cap)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ConversationError("worktree-failed", f"git {argv[0]} did not finish: {exc}", code=1,
                                        fix="repeat conversation.create with the same request_id") from exc

        adopted = False
        if target.exists():
            head = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=target) if (target / ".git").is_file() else None
            if head is not None and head.returncode == 0 and head.stdout.strip() == branch:
                adopted = True
            else:
                # Not a worktree on this branch (an add cut short). No turn ever ran
                # in it: a conversation dispatches only once its worktree is recorded.
                shutil.rmtree(target, ignore_errors=True)
                git("worktree", "prune")
        if not adopted:
            has_branch = git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
            argv = ["worktree", "add", str(target), branch] if has_branch else ["worktree", "add", "-b", branch, str(target)]
            made = git(*argv, cap=self._git_timeout_s("worktree_add_timeout_s"))
            if made.returncode:
                raise ConversationError("worktree-failed", made.stderr.strip()[-300:] or "git worktree add failed",
                                        code=1, fix="repeat conversation.create with the same request_id")
            os.chmod(target, 0o700)
        base = git("rev-parse", "--verify", "HEAD", cwd=target)
        # A conversation started in a subdirectory works in the same subdirectory.
        relative = os.path.relpath(source, top)
        inside = target / relative if relative != "." and not relative.startswith("..") else target
        workspace = inside if inside.is_dir() else target
        record = {"path": str(target), "branch": branch, "source": source, "repository": top,
                  "base": base.stdout.strip() if base.returncode == 0 else None, "created_at": utcnow()}
        return self.store.update_conversation(cid, workspace=str(workspace), worktree=record)

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
        return {"conversation": self._view_live(self.store.update_conversation(conversation["conversation_id"], **fields))}

    def op_conversation_unblock(self, args, peer) -> dict:
        verdict = self._person(peer, "unblocking an unfinished turn")
        conversation = self.store.conversation(args["conversation_id"])
        if args.get("confirm") is not True or args.get("choice") not in ("continue", "leave"):
            raise ConversationError("confirm", "unblock needs choice continue|leave and confirm: true")
        if conversation["blocked_by"] not in ("unfinished-turn", "delivery-unknown"):
            if conversation.get("legacy_hold"):
                # C-30.4: the import's own hold; only a pass that finds the session settled lifts it.
                raise ConversationError(LEGACY_OWNER, f"the legacy cockpit may be using this session: "
                                        f"{conversation['legacy_hold']}", code=7,
                                        fix="settle it in the cockpit, then run python -m subfleet.importer "
                                            "--legacy-cockpit")
            raise ConversationError("not-blocked", "the conversation is not blocked by an unfinished turn")
        if conversation["blocked_by"] == "delivery-unknown":
            raise ConversationError("resolve-first", "resolve the delivery-unknown message first",
                                    fix="message.resolve")
        self._record_note(conversation, args["choice"], verdict)
        return {"conversation": self._view_live(self.store.update_conversation(conversation["conversation_id"],
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
        if conversation["workspace_kind"] == "worktree" and not conversation.get("worktree"):
            raise ConversationError("worktree-missing", "the conversation's worktree was not created",
                                    fix="repeat conversation.create with the same request_id")
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
                out.append(self._receipt(self.store.message(canonical_uuid(mid)), text=True))
            except ConversationError:
                out.append({"message_id": mid, "state": "unknown"})
        return {"messages": out}

    def op_message_cancel(self, args, peer) -> dict:
        """IR-2, IR-7: withdraw only before the provider could have seen it."""
        with self._stops:
            return self._cancel(args)

    def _cancel(self, args) -> dict:
        message_id = canonical_uuid(args["message_id"])
        try:
            message = self.store.message(message_id)
        except ConversationError:
            return self._tombstone(message_id, args.get("conversation_id"))
        if message["state"] in (QUEUED, WAITING) and not message["job_id"] and not self._turn_job(message):
            # No job carries it (queued, or waiting to be submitted again): the
            # conversation store decides, in one transaction that also records the
            # person's stop. A job the dispatcher submits meanwhile finds the
            # message cancelled and is cancelled in turn (_dispatch_one); a runner
            # admission launched for it first reads that stop and never writes the
            # message (IR-2). A withdrawal that loses to the job's binding (the
            # daemon adopting it) records nothing: the refused cancel leaves no stop
            # that a runner starting meanwhile could act on (review of 3c1a34e,
            # finding 3).
            if self._withdraw(message_id, expect=(QUEUED, WAITING), unbound=True):
                self._cancel_late_job(message_id)
                return self._receipt(self.store.message(message_id))
            message = self.store.message(message_id)
        if message["state"] in (QUEUED, WAITING):
            job = self._turn_job(message)
            if job and self._cancel_job_without_attempt(job["job_id"]):
                self._withdraw(message_id, expect=(QUEUED, WAITING, STARTING))
                return self._receipt(self.store.message(message_id))
            if job is None and self.store.message(message_id)["state"] == WAITING:
                # Claimed by the dispatcher, whose job for it is being created (C-24.7).
                raise ConversationError("dispatching", "the message is being handed to its turn job",
                                        fix="send the cancel again in a moment")
        # Refused, with no stop recorded: a stop would outlive the refusal (a runner
        # or a later restart would read it and stop a turn the person was told runs on).
        raise ConversationError("too-late", "the provider may already have this message", fix="use turn.interrupt")

    def _handover(self, message_id: str) -> threading.Lock:
        """The lock a stop of `message_id` is recorded under, and its runner hands
        the message frame over under (`TurnRunner._handover_verdict`, C-24.7)."""
        return self._handovers[hash(message_id) % len(self._handovers)]

    def _withdraw(self, message_id: str, *, expect: tuple[str, ...], unbound: bool = False) -> bool:
        """Withdraw a message with the person's stop, in one conversation-store
        transaction (`ConversationStore.withdraw`), under its handover lock."""
        with self._handover(message_id):
            return self.store.withdraw(message_id, expect=expect, stop_at=utcnow(), unbound=unbound)

    def _cancel_late_job(self, message_id: str) -> None:
        """Cancel a turn job the dispatcher made for a message just withdrawn, while it has no attempt."""
        job = self._turn_job(self.store.message(message_id))
        if job:
            self._cancel_job_without_attempt(job["job_id"])

    def _tombstone(self, message_id: str, conversation_id: str | None) -> dict:
        if not conversation_id:
            raise ConversationError("unknown-message", "cancel of an unknown message needs its conversation_id")
        conversation = self.store.conversation(conversation_id)
        last = self.store.one("SELECT message_id FROM messages WHERE conversation_id=? AND origin='person' "
                              "ORDER BY seq DESC LIMIT 1", (conversation_id,))
        # Born cancelled, in one transaction: the dispatcher never sees it queued.
        message, _ = self.store.submit_message(conversation_id=conversation_id, message_id=message_id,
                                               after_message_id=last["message_id"] if last else None,
                                               text="(withdrawn before it was received)", attachments=[],
                                               settings=conversation["settings"], origin="tombstone",
                                               state=CANCELLED, state_reason="withdrawn-before-receipt")
        return self._receipt(message)

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
            if not changed:
                return False
            tx.execute("DELETE FROM leases WHERE holder=?", (job_id,))
        daemon._notify()
        return bool(changed)

    def op_turn_interrupt(self, args, peer) -> dict:
        with self._stops:
            return self._interrupt(args)

    def _interrupt(self, args) -> dict:
        message_id = canonical_uuid(args["message_id"])
        message = self.store.message(message_id)
        if message["state"] not in (STARTING, RUNNING, APPROVAL_NEEDED, WAITING):
            raise ConversationError("not-running", f"the message is {message['state']}")
        with self._handover(message_id):
            # Recorded before the runner is looked up (a runner started meanwhile
            # reads it), and under the handover lock: a runner about to write the
            # message sees it first, or has already handed the message over (C-24.7).
            self.store.update_message(message_id, stop_requested_at=utcnow())
        runner = self._runner_for_message(message_id)
        if runner is not None:
            runner.interrupt("stopped")
        elif message["state"] == WAITING:
            job = self._turn_job(message)
            if job and self._cancel_job_without_attempt(job["job_id"]):
                self.store.set_state(message_id, CANCELLED, reason="withdrawn", expect=(WAITING,))
            elif job is None:
                # Waiting to be submitted again (a re-admission or a deferral): no
                # provider has it. The dispatcher also withdraws a stopped message.
                self.store.set_state(message_id, CANCELLED, reason="withdrawn", expect=(WAITING,), unbound=True)
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
        if not self.store.set_state(message_id, FAILED, reason=f"resolved-{args['resolution']}",
                                    expect=(DELIVERY_UNKNOWN,), resolution=record):
            raise ConversationError("not-ambiguous", "the message was resolved meanwhile")
        conversation = self.store.conversation(message["conversation_id"])
        if conversation["blocked_by"] == "delivery-unknown":
            fields: dict[str, Any] = {"blocked_by": None}
            if args["resolution"] == "delivered" and conversation["provider"] == "claude":
                # C-24.8: the person says Claude has the message and its turn ended
                # with no `result`, so the next resume could continue it: the person
                # chooses next (`conversation.unblock`). The session it named exists.
                fields["blocked_by"] = "unfinished-turn"
                attempt = self.daemon.store.one("SELECT job_id, seq FROM attempts WHERE job_id=? ORDER BY seq DESC "
                                                "LIMIT 1", (message.get("job_id"),)) if message.get("job_id") else None
                turn = (read_turn(self.root / "jobs" / attempt["job_id"] / f"a{attempt['seq']}") if attempt else None) or {}
                if turn.get("native_session_id") and not conversation["native_session_id"]:
                    fields["native_session_id"] = turn["native_session_id"]
            self.store.update_conversation(conversation["conversation_id"], **fields)
        return self._receipt(self.store.message(message_id))

    def _receipt(self, message: dict, *, created: bool | None = None, text: bool = False) -> dict:
        out = {k: message.get(k) for k in ("message_id", "conversation_id", "seq", "origin", "continues", "state",
                                           "state_reason", "settings", "served", "turn_ref", "updated_at")}
        out["stop_requested"] = bool(message.get("stop_requested_at"))
        if created is not None:
            out["created"] = created
        if text:
            # C-25.2: a client that did not send the message (another window, a
            # restarted app, the CLI) still shows what the person wrote.
            try:
                body = self.store.message_text(message)
            except OSError:
                body = None
            if body is not None:
                out["text"] = body[:RECEIPT_TEXT_CHARS]
                out["text_truncated"] = len(body) > RECEIPT_TEXT_CHARS
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

    # --- ops: changes (C-26.14, design D-25) ------------------------------------

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
        cap = self._git_timeout_s()
        try:
            if end is None:
                now = turn_diff.snapshot(workspace, timeout_s=cap)
                if now is None:
                    turn_diff.checkout(workspace, timeout_s=cap)     # raises `workspace-gone`
                    return _unavailable("no-snapshot", "the workspace's checkout has no commit to snapshot")
                end = {"tree": now[1], "head": now[0], "live": True, "at": utcnow()}
            result = turn_diff.build(workspace, start["tree"], end["tree"], path=path, timeout_s=cap)
        except turn_diff.Unavailable as exc:
            return _unavailable(exc.reason, str(exc))
        except SalvageError as exc:
            raise ConversationError("diff-failed", str(exc), code=1,
                                    fix="try again" if exc.transient else "check the workspace's repository")
        return {"available": True, "path": path, "from": start, "to": end, **result}

    def record_trees(self, turn: dict, attempt: dict, receipt: dict) -> None:
        """The daemon's finalization seam: a turn attempt's end (C-26.10, C-26.14)."""
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
                # Older handoffs could die after their audited cancel but before
                # leaving a fence. A retry still owns those pending messages.
                self._lift_fence(cid, f"{HANDOFF_FENCE}{request_id}")
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
                        prepared, withdrawals=[{"message_id": step["message"]["message_id"], "expect": step["expect"],
                                                "unbound": step.get("unbound", False), "not_reason": step.get("not_reason")}
                                               for step in plan],
                        fence=(cid, fence) if fence else None)
                except BaseException:
                    self.store.discard_handoff(prepared)
                    raise
        finally:
            if cid:
                # Even discarding prepared files or returning an existing request
                # can fail. Recovery must run before releasing the source.
                try:
                    if fence:
                        self._lift_fence_quietly(cid, fence)
                finally:
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
        restores = []
        for message in self.store.query(
                "SELECT * FROM messages WHERE conversation_id=? AND state IN (?,?,?) ORDER BY seq",
                (cid, QUEUED, WAITING, CANCELLED)):
            if str(message["state_reason"] or "").startswith("handed-off:"):
                continue
            # A crash may precede the dispatcher's job binding. Request ids, not
            # the optional binding, identify the job that carried this turn.
            job = self._turn_job(message)
            if job and self._cancelled_by(job["job_id"], marker):
                restores.append({"message_id": message["message_id"], "job_id": job["job_id"],
                                 "turn_seq": message["turn_seq"]})
        if not restores and self.store.conversation(cid)["blocked_by"] != fence:
            return []
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
                if state == WAITING and message.get("state_reason") == CLAIMED:
                    raise ConversationError("live-turn", "the source's next message is being dispatched",
                                            fix="send the same request again in a moment")
                # Queued, or waiting with no job (readmitted, deferred, held for another
                # writer): it leaves under the conversation store's guard, still in this
                # state, unbound and unclaimed when the handoff commits (a deferred
                # readmission no longer blocks every handoff; review of 6290a51).
                plan.append({"message": message, "expect": (state,), "job_id": None,
                             "unbound": True, "not_reason": CLAIMED})
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
            "SELECT 1 FROM events e JOIN jobs j USING(job_id) "
            "WHERE e.kind='job.cancel_requested' AND j.job_id=? AND j.state='cancelled' "
            "AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.job_id=j.job_id) "
            "AND json_extract(data_json,'$.by')=? AND json_extract(data_json,'$.request_id')=?",
            (job_id, marker["by"], marker["request_id"])))

    # --- ops: attachments, catalog ---------------------------------------------

    def op_attachment_add(self, args, peer) -> dict:
        return attachment_store.add(self.store, args.get("path"), args.get("sha256"))

    def op_catalog_refresh(self, args, peer) -> dict:
        """D-23: start a run now; the handler never waits for it (C-25.3)."""
        from .catalog import read_catalog
        requested = self._start_catalog()
        generated = read_catalog(self.root, limit=1)["generated_at"]
        return {"requested": requested["requested"], "running": requested["running"], "generated_at": generated}

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
        """Called on the control loop's worker pool, never on a request thread. Each
        step is independent: one that fails is logged and the others still run."""
        # A handoff's fence is lifted before anything is dispatched (C-30.3, D-18).
        for step in (self._lift_stale_fences, self._catalog_tick, self._dispatch, self._adopt_runners, self._settle_unstarted,
                     self._reap_runners, self._compact):
            if self._closed:
                return                          # a tick close() overtook: its store is gone
            try:
                step()
            except Exception as exc:
                self.log.error("conversation tick step %s failed: %s: %s", step.__name__, type(exc).__name__, exc)

    # --- the catalog timer (C-30.1, design D-23) --------------------------------

    def _catalog_tick(self) -> None:
        """Start a catalog run every `catalog_interval_s`. Never waits for one: the
        run is a separate process this tick only starts, reaps, or stops when it
        outlives `CATALOG_KILL_AFTER_S`."""
        self._reap_catalog()
        interval = float(self.config()["catalog_interval_s"])
        if interval <= 0:
            return                              # policy: on request only
        last = self._catalog_last
        if self._catalog_proc is None and (last is None or self.clock() - last >= interval):
            self._start_catalog()

    def _catalog_stale_after_s(self) -> float:
        """Three missed runs; with the timer off, three of the default interval."""
        interval = float(self.config()["catalog_interval_s"]) or float(CONVERSATION_DEFAULTS["catalog_interval_s"])
        return 3 * interval

    def _start_catalog(self) -> dict:
        from .catalog import fence_pipe, refresh_running, spawn_refresh
        with self._catalog_lock:
            if self._closed:
                # A tick or request close() overtook: nothing would stop or reap a run
                # started now, and its root may be on its way out (_stop_catalog).
                return {"requested": False, "running": False}
            self._reap_catalog()
            if self._catalog_proc is not None:
                return {"requested": False, "running": True}
            self._catalog_last = self.clock()
            try:
                if self._catalog_fence is None:
                    self._catalog_fence = fence_pipe()
                process = spawn_refresh(self.root, fence_fd=self._catalog_fence[0])
            except OSError as exc:
                self.log.warning("catalog run not started: %s", exc)
                return {"requested": False, "running": bool(refresh_running(self.root))}
            if process is None:                 # another run holds the lock
                return {"requested": False, "running": bool(refresh_running(self.root))}
            self._catalog_proc, self._catalog_started, self._catalog_killed = process, self.clock(), False
            return {"requested": True, "running": True}

    def _reap_catalog(self) -> None:
        from .catalog import DECLINED
        with self._catalog_lock:
            process = self._catalog_proc
            if process is None:
                return
            if process.poll() is not None:
                self._catalog_proc = None
                if process.returncode and not self._catalog_killed:
                    # A run this service still tracks declined while the service is open.
                    why = DECLINED.get(process.returncode)
                    if why:
                        self.log.warning("catalog run %s stopped publishing (exit %s): %s", process.pid,
                                         process.returncode, why)
                    else:
                        self.log.warning("catalog run exited %s", process.returncode)
                return
            if self._catalog_killed or self.clock() - self._catalog_started < CATALOG_KILL_AFTER_S:
                return
            # Still unreaped, so its pid (and the group it leads, start_new_session)
            # cannot have been reused: the signal reaches only this run. A later
            # tick reaps it; nothing here waits for it.
            self.log.warning("catalog run %s outlived %g s; stopping it", process.pid, CATALOG_KILL_AFTER_S)
            self._catalog_killed = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _stop_catalog(self) -> None:
        """close(): end this service's catalog run before returning, and start none
        after (C-30.1). A run that outlived its daemon recreated a state root its
        owner had just removed, or wrote into it while `rmtree` was emptying it
        (2026-09-25). Closing the fence first means a run that somehow survives
        the signals still writes nothing (`catalog.Owner`)."""
        with self._catalog_lock:
            self._closed = True
            process, self._catalog_proc = self._catalog_proc, None
            fence, self._catalog_fence = self._catalog_fence, None
        if fence is not None:
            os.close(fence[1])
        try:
            if process is None or process.poll() is not None:
                return
            for sig in (signal.SIGTERM, signal.SIGKILL):
                # Unreaped until a wait returns, so the group it leads is still its own.
                try:
                    os.killpg(process.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    process.wait(timeout=CATALOG_STOP_WAIT_S)
                    return
                except subprocess.TimeoutExpired:
                    continue
            self.log.warning("catalog run %s did not end within %g s of SIGKILL", process.pid, CATALOG_STOP_WAIT_S)
        finally:
            if fence is not None:
                os.close(fence[0])

    # --- compaction (C-25.4, review IR-6) ---------------------------------------

    def _compact(self) -> None:
        """Remove the delta rows of attempts that have ended, whose message settled
        at least `compact_after_s` ago, and that no live runner still reads."""
        config = self.config()
        cutoff = _iso_ago(float(config["compact_after_s"]))
        live = {aid for aid, runner in list(self.runners.items()) if not runner.finished.is_set()}
        for row in self.store.compactable(settled_before=cutoff, limit=int(config["compact_per_tick"])):
            if row["attempt_id"] in live:
                continue
            attempt = self.daemon.store.one("SELECT state FROM attempts WHERE attempt_id=?", (row["attempt_id"],))
            # An attempt row retention already removed belonged to a job that had ended.
            if attempt is not None and attempt["state"] not in ATTEMPT_ENDED:
                continue
            self.store.compact(row["attempt_id"])

    # --- runners and retention (C-26.12, review IR-17) --------------------------

    def _reap_runners(self) -> None:
        """Forget runners that have finished once the job store has ended their
        attempt; until then `_adopt_runners` must keep seeing them."""
        for aid, runner in list(self.runners.items()):
            if not runner.finished.is_set():
                continue
            attempt = self.daemon.store.one("SELECT state FROM attempts WHERE attempt_id=?", (aid,))
            if attempt is None or attempt["state"] not in ("starting", "running", "finalizing"):
                self.runners.pop(aid, None)

    def retention_pins(self) -> set[str]:
        """Turn jobs retention must keep (C-26.12): every turn job of a message that
        is not terminal, of a blocked conversation, and of a live runner. Read-only
        and cheap; retention asks again inside each delete transaction."""
        jobs = self.daemon.store
        pinned = {aid.rsplit("/", 1)[0] for aid, runner in list(self.runners.items()) if not runner.finished.is_set()}
        terminal = ",".join("?" * len(TERMINAL_STATES))
        for row in self.store.query(f"SELECT message_id, job_id FROM messages WHERE state NOT IN ({terminal})",
                                    TERMINAL_STATES):
            if row["job_id"]:
                pinned.add(row["job_id"])
            # Every turn job the message ever had (`turn:<id>:<n>`), by the unique index.
            prefix = f"turn:{row['message_id']}:"
            pinned.update(r["job_id"] for r in jobs.query(
                "SELECT job_id FROM jobs WHERE request_id>=? AND request_id<?", (prefix, prefix[:-1] + ";")))
        for row in self.store.query("SELECT conversation_id FROM conversations WHERE blocked_by IS NOT NULL"):
            pinned.update(r["job_id"] for r in jobs.query("SELECT job_id FROM jobs WHERE kind='turn' AND name=?",
                                                          (f"turn-{row['conversation_id']}",)))
        return pinned

    # --- dispatch (design §4) ---------------------------------------------------

    def admission_hold(self, job: dict) -> dict | None:
        """Why admission must not place this turn job now (C-24.5, C-30.4), or None.

        A turn job is created only for a conversation that is not blocked, but it
        can be blocked after: the legacy import holds a conversation while the
        daemon is stopped, with its turn job already queued. The job then waits
        here, placing nothing, until both blocks are clear.
        """
        path = self.root / "jobs" / job["job_id"] / "manifest.json"
        try:
            manifest, error = json.loads(path.read_bytes()), None
        except (OSError, ValueError) as exc:          # missing, unreadable, not JSON
            manifest, error = None, type(exc).__name__
        turn = manifest.get(TURN_MANIFEST_KEY) if isinstance(manifest, dict) else None
        if not (isinstance(turn, dict) and isinstance(turn.get("conversation_id"), str)
                and isinstance(turn.get("message_id"), str) and turn.get("provider") in ("claude", "codex")):
            # C-6.12: a turn job whose manifest does not name its conversation,
            # message and provider is this job's problem, held here, never the
            # pass's: admission reads those fields again to reserve it.
            return {"reason": "conversation-blocked", "conversation_id": None,
                    "error_type": error or "manifest", "error": "its turn manifest cannot be read"}
        conversation_id = turn["conversation_id"]
        message = self.store.one("SELECT state FROM messages WHERE message_id=?", (turn["message_id"],))
        if message and message["state"] in TERMINAL_STATES:
            # IR-2: its message was withdrawn (or settled) after the job was
            # made; the job is cancelled while it has no attempt, never run.
            self._cancel_job_without_attempt(job["job_id"])
            return {"reason": "message-settled", "conversation_id": conversation_id, "state": message["state"]}
        hold = self.store.turn_hold(conversation_id)
        if hold:
            return {"reason": "conversation-blocked", "conversation_id": conversation_id, **hold}
        return None

    def bound_session(self, session_id: str | None) -> dict | None:
        """C-26.3: the conversation a native session is bound to, of either provider."""
        if not session_id:
            return None
        return self.store.by_native("claude", session_id) or self.store.by_native("codex", session_id)

    def bound_sessions(self) -> list[str]:
        """Every native session a conversation is bound to (C-26.3, design D-17)."""
        return sorted({canonical_native(row["native_session_id"]) for row in self.store.query(
            "SELECT native_session_id FROM conversations WHERE native_session_id IS NOT NULL")})

    def _turn_job(self, message: dict) -> dict | None:
        """The main store decides which job carries a message (IR-1)."""
        row = self.daemon.store.one("SELECT * FROM jobs WHERE request_id=? AND kind='turn'",
                                    (f"turn:{message['message_id']}:{message['turn_seq']}",))
        return dict(row) if row else None

    def _dispatch(self) -> None:
        candidates = self.store.next_dispatchable() + self._resubmittable()
        seen = {m["message_id"] for m in candidates}
        with self._lock:
            self._deferred = {mid: value for mid, value in self._deferred.items() if mid in seen}
        now = self.clock()
        for message in candidates:
            deferred = self._deferred.get(message["message_id"])
            if deferred and now < deferred[1]:
                continue                        # backing off (C-26.1)
            try:
                self._dispatch_one(message)
            except Exception as exc:            # one message never holds up the others
                self.log.error("dispatch of %s failed: %s: %s", message["message_id"], type(exc).__name__, exc)

    def _dispatch_one(self, message: dict) -> None:
        mid = message["message_id"]
        cid = message["conversation_id"]
        conversation = self.store.conversation(cid)
        if conversation.get("legacy_hold"):
            # C-30.4: the legacy import holds it; nothing is submitted, bound or
            # rewritten until a pass lifts the hold (a person may still withdraw
            # the message: message.cancel, turn.interrupt).
            return
        job = self._turn_job(message)
        if job is None and message.get("stop_requested_at"):
            # Stopped by a person while waiting to be submitted again: nothing was sent.
            self.store.set_state(mid, CANCELLED, reason="withdrawn", expect=(QUEUED, WAITING), unbound=True)
            return
        if job is None and conversation["workspace_kind"] == "worktree" and not conversation.get("worktree"):
            # Never run a worktree conversation in the checkout it was cut from.
            self._defer(message, "the conversation's worktree was not created; repeat conversation.create")
            return
        if job is None and not self._previous_released(conversation, message):
            return
        if job is None and conversation["provider"] == "claude" and conversation.get("native_session_id"):
            from . import catalog
            holders = catalog.external_writers(conversation["native_session_id"])
            if holders:
                self._hold_for_writer(message, holders)
                return
        # C-24.7, IR-2, IR-28: the handoff's ownership is shared only through the
        # durable claim, never through the file work in submit: either the handoff
        # owns the source first, or its plan sees the claim and refuses.
        with self._lock:
            if cid in self._handing_off:
                return
            conversation = self.store.conversation(cid)
            if conversation["blocked_by"] or conversation.get("legacy_hold") or conversation["archived_at"]:
                return                          # readmissions obey the durable fence and the legacy hold too
            message = self.store.message(mid)
            if message["state"] == QUEUED:
                # A rollback since the scan may have restored an earlier message or
                # given this one a new turn sequence.
                eligible = self.store.next_dispatchable(cid)
                if not eligible or eligible[0]["message_id"] != mid:
                    return
            elif not (message["state"] == WAITING and message["job_id"] is None):
                return
            prior_state, prior_reason = message["state"], message.get("state_reason")
            job = self._turn_job(message)
            claimed = False
            if job is None:
                # Claimed in the conversation store before its job exists: a
                # withdrawal and this claim are one store's transactions. A claim a
                # crash interrupted is resumed as it stands.
                recovered = prior_reason == CLAIMED
                if not recovered and not self.store.set_state(mid, WAITING, reason=CLAIMED, expect=(prior_state,),
                                                              unbound=True, expect_turn_seq=message["turn_seq"]):
                    return
                claimed = True
        if job is None:
            try:
                job = self._submit_turn(conversation, message)
            except Exception as exc:
                permanent, why = _refusal(exc)
                if permanent:
                    # The daemon refused the turn outright: it never reached a provider.
                    with self._lock:
                        self._deferred.pop(mid, None)
                    self.store.set_state(mid, FAILED, reason=f"not-delivered: {why}"[:200], expect=(QUEUED, WAITING),
                                         expect_turn_seq=message["turn_seq"])
                else:
                    if claimed and self._turn_job(message) is None:
                        # The claim is released, so the message can still be withdrawn
                        # or handed off while it waits; a claim recovered after a crash
                        # goes back to `queued` (review of 6290a51, finding 2).
                        back = QUEUED if prior_state == QUEUED or prior_reason == CLAIMED else WAITING
                        self.store.set_state(mid, back, reason=None if back == QUEUED else prior_reason,
                                             expect=(WAITING,), unbound=True, expect_turn_seq=message["turn_seq"])
                    self._defer(self.store.message(mid), why)
                    if not isinstance(exc, (protocol.ProtocolError, AdapterError, ConversationError, OSError,
                                            sqlite3.Error)):
                        self.log.error("turn submit for %s failed: %s: %s", mid, type(exc).__name__, exc)
                return
        with self._lock:
            self._deferred.pop(mid, None)
        reason = prior_reason
        if reason and not reason.startswith("readmit:"):
            reason = None                       # a claim or a deferral is over once the job exists
        if self.store.set_state(mid, WAITING, reason=reason, expect=(QUEUED, WAITING), job_id=job["job_id"],
                                expect_turn_seq=message["turn_seq"]):
            return
        # The message moved while its job was being submitted: a person withdrew it
        # (message.cancel), or a handoff rolled back and gave it a new turn sequence.
        # Its job must not run (IR-2): cancelled outright while it has no attempt,
        # else flagged, which `_launch` re-reads before starting.
        now = self.store.message(mid)
        if (now["state"] == CANCELLED or now["turn_seq"] != message["turn_seq"]) \
                and not self._cancel_job_without_attempt(job["job_id"]):
            with self.daemon.store.transaction("job.cancel_requested", job_id=job["job_id"]) as tx:
                tx.execute("UPDATE jobs SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE job_id=?",
                           (utcnow(), job["job_id"]))
            self.daemon._notify()

    def _provider_tries(self, message: dict) -> int:
        """How many of this message's turn jobs reached a provider (have an attempt).
        This, not `turn_seq`, counts readmissions (C-24.6): a handoff that rolled back
        cancelled its job before any attempt and so adds nothing, though it moved the
        message to a new turn sequence (review of 6290a51, finding 3)."""
        prefix = f"turn:{message['message_id']}:"
        return self.daemon.store.one(
            "SELECT count(DISTINCT j.job_id) AS n FROM jobs j JOIN attempts a ON a.job_id=j.job_id "
            "WHERE j.request_id>=? AND j.request_id<?", (prefix, prefix[:-1] + ";"))["n"]

    def _hold_for_writer(self, message: dict, holders: list[int]) -> None:
        """C-26.3, D-17: a Claude process outside Subfleet holds the session, so a
        turn now would be a second writer on one transcript. The message waits,
        says so, and is looked at again every EXTERNAL_WRITER_RECHECK_S. It is a
        wait, not a refusal: it adds nothing to a deferral's backoff."""
        mid = message["message_id"]
        with self._lock:
            count = self._deferred.get(mid, (0, 0.0))[0]
            self._deferred[mid] = (count, self.clock() + EXTERNAL_WRITER_RECHECK_S)
        reason = "external-writer: pid " + ", ".join(str(pid) for pid in holders)
        if message["state"] != WAITING or message.get("state_reason") != reason:
            self.store.set_state(mid, WAITING, reason=reason, expect=(QUEUED, WAITING), unbound=True)

    def _defer(self, message: dict, why: str) -> None:
        """A submit refused before any provider saw the message: it keeps waiting,
        says why, and is not submitted again until its backoff passes."""
        mid = message["message_id"]
        with self._lock:
            count = self._deferred.get(mid, (0, 0.0))[0] + 1
            delay = min(DEFER_MAX_S, DEFER_BASE_S * 2 ** (count - 1))
            self._deferred[mid] = (count, self.clock() + delay)
        reason = f"deferred: {why}"[:200]
        if message.get("state_reason") != reason:
            self.store.set_state(mid, message["state"], reason=reason, expect=(message["state"],))
        if count & (count - 1) == 0:            # 1, 2, 4, 8, ...: the log stays bounded
            self.log.warning("turn submit for %s deferred (%d in a row, next try in %g s): %s",
                             mid, count, delay, why)

    def _resubmittable(self) -> list[dict]:
        """Waiting messages with no job bound: re-admitted after a failure that
        provably never delivered them (IR-1, IR-23), deferred on their way back, or
        claimed when a crash cut the dispatcher short. `_dispatch_one` binds a job
        that exists for the message's turn sequence, and submits one that does not
        (design §4's repair)."""
        rows = self.store.query("SELECT * FROM messages WHERE state='waiting' AND job_id IS NULL")
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
                "affinity_lane": affinity, "digest": message["digest"],
                "network": bool((self.daemon.policy.get("network") or {}).get("codex_workspace_write", False))}
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
            held = self._writer_check(turn, adir)
            if held:
                spec = dataclasses.replace(spec, held_by=tuple(held))
            # Read before the runner is registered: a runner registered and never
            # started would never be adopted again (C-30.4, D-17).
            legacy = (self.store.turn_hold(turn["conversation_id"]) or {}).get("legacy_hold")
            runner = TurnRunner(store=self.store, attempt=dict(attempt), spec=spec,
                                conversation_id=turn["conversation_id"], attempt_dir=adir,
                                control_socket=start["control_socket"], on_outcome=self._on_outcome,
                                on_contain=self._on_contain, log=self.log,
                                clocks=Clocks.from_policy(self.daemon.policy),   # C-24.7, C-26.5, C-26.9
                                on_catalog=self._on_catalog, handover=self._handover(turn["message_id"]))
            if legacy:
                # C-30.4, D-17: a pass run while the daemon was down found the
                # legacy cockpit may be using this session again. A turn that
                # kept running across the restart is stopped, not left beside
                # it: before its message is ever written if the relay log shows
                # it was not handed over, else through D-13.
                self.log.warning("turn %s stopped: its conversation is held %s (%s)", aid, LEGACY_OWNER, legacy)
                runner.withhold(LEGACY_OWNER)
            with self._lock:
                if self._closed:
                    return                  # close() overtook: nothing would stop a runner started now
                self.runners[aid] = runner
                try:
                    self.store.set_state(turn["message_id"], STARTING, expect=(WAITING,), job_id=attempt["job_id"])
                finally:
                    runner.start()
            try:
                self._record_start(turn, dict(attempt))   # C-26.14: the turn's diff has a base
            except Exception as exc:                      # finalization records it again; a turn never waits on it
                self.log.warning("turn %s start snapshot not recorded: %s: %s", aid, type(exc).__name__, exc)

    def _writer_check(self, turn: dict, adir: Path) -> list[int]:
        """C-26.3 at launch: dispatch looked before the job waited for admission,
        and a Claude process may have taken the session since. Decided once per
        attempt, before anything is written, and kept, so a replay after a
        restart makes the same choice."""
        path = adir / "held_by.json"
        recorded = _read_json(path)
        if isinstance(recorded, dict):
            return [int(pid) for pid in recorded.get("pids") or []]
        if turn.get("provider") != "claude" or not turn.get("native_session_id") or (adir / "stdin.jsonl").exists():
            return []
        from . import catalog
        pids = catalog.external_writers(turn["native_session_id"])
        path.write_text(json.dumps({"pids": pids, "at": utcnow()}) + "\n")
        return pids

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
                                 expect=(WAITING, STARTING), expect_turn_seq=message["turn_seq"])

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
        """Settle a message from its turn (D-12, D-14, C-24.6, C-24.8, C-26.7).
        The decision is `reconcile.settle`'s; this applies it."""
        turn = read_turn(runner.adir) or {}
        message = self.store.message(runner.message_id)
        conversation = self.store.conversation(runner.conversation_id)
        provider = conversation["provider"]
        # The readmissions so far: this message's turn jobs a provider reached,
        # less the one settling now (review of 6290a51, finding 3).
        settlement = reconcile.settle(
            turn, provider=provider, turn_seq=max(0, self._provider_tries(message) - 1),
            gather=lambda: reconcile.gather(provider, runner.message_id, runner.adir, turn),
            person_stopped=bool(message.get("stop_requested_at")))
        served = {**(message.get("served") or {}), **(turn.get("served") or {}),
                  "lane_id": runner.attempt.get("lane_id"), "model": turn.get("served_model")}
        native = turn.get("native_session_id")
        # C-24.1: a Codex thread id comes only from the server's answer. A Claude
        # session id is minted here, so it is kept only once the provider holds
        # the session; until then the next turn starts it with `--session-id`
        # rather than resuming a session the provider may never have created.
        if native and not conversation["native_session_id"] and (provider == "codex" or settlement.session_known):
            self.store.update_conversation(conversation["conversation_id"], native_session_id=native,
                                           **({"lane_id": runner.attempt.get("lane_id")}
                                              if provider == "codex" else {}))
        live = (STARTING, RUNNING, APPROVAL_NEEDED, WAITING)
        self.store.withdraw_approvals(attempt_id=runner.attempt_id)
        if settlement.delivery is not None:
            # Why the message settled as it did, for the person (D-24).
            self.store.append_events(
                conversation_id=runner.conversation_id, message_id=runner.message_id, attempt_id=runner.attempt_id,
                events=[("command", "cmd:reconcile", 0, "status",
                         {"phase": "reconciled", "delivery": settlement.delivery,
                          "evidence": settlement.evidence.as_dict() if settlement.evidence else None})],
                stdout_offset=runner.offset, stdin_seq=runner.next_seq - 1)
        if settlement.continue_elsewhere and message["state"] in live:
            # C-26.7: the continuation is written first, so no reader sees the
            # limited message failed without it; it cannot dispatch while the
            # original is live.
            self._continue_elsewhere(conversation, message)
        if settlement.readmit:
            self.store.set_state(message["message_id"], WAITING, reason=settlement.reason, expect=live,
                                 turn_seq=message["turn_seq"] + 1, job_id=None)
            if settlement.reason == "readmit:external-writer" and provider == "codex":
                # Only a provider start can see a Codex thread's active turn: space them.
                with self._lock:
                    self._deferred[message["message_id"]] = (0, self.clock() + CODEX_WRITER_RECHECK_S)
        else:
            fields: dict[str, Any] = {"served": served}
            if settlement.state == COMPLETE:
                fields["turn_ref"] = turn.get("turn_id") or message.get("turn_ref")
            self.store.set_state(message["message_id"], settlement.state, reason=settlement.reason, expect=live,
                                 **fields)
        if settlement.block:
            self.store.update_conversation(conversation["conversation_id"], blocked_by=settlement.block)
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
        if chain >= 2 or self.store.one("SELECT 1 FROM messages WHERE continues=?", (message["message_id"],)):
            return                              # at most two, and one per limited message
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
                              unified_exec_off=codex_turn.unified_exec_off(turn["settings"]["permission"],
                                                                           bool(turn.get("network"))))
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


def _refusal(exc: BaseException) -> tuple[bool, str]:
    """A turn submit that raised before any job existed: (permanent, why).

    Permanent is the daemon's own refusal of this message as it is (invalid
    input, exit 2, or a policy refusal, exit 7: a checkout on main without
    `allow_main`, a missing attachment, a model the fleet does not route).
    Everything else (an operational failure, exit 1, such as git timing out while
    inspecting the workspace; a store or file error; a defect) may pass on a later
    try, so the message waits.
    """
    if isinstance(exc, ConversationError):
        return exc.code != int(Exit.OPERATIONAL), exc.reason
    if isinstance(exc, (AdapterError, protocol.ProtocolError)):
        return int(exc.code) != int(Exit.OPERATIONAL), str(exc) or type(exc).__name__
    return False, f"{type(exc).__name__}: {exc}"


def _iso_ago(seconds: float) -> str:
    """UTC ISO time `seconds` ago, in the conversation store's format."""
    stamp = datetime.now(UTC) - timedelta(seconds=seconds)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def _unavailable(reason: str, detail: str) -> dict:
    """A diff with nothing to compare: the same shape, empty, and why (C-26.14)."""
    return {"available": False, "reason": reason, "detail": detail, "files": [], "files_truncated": False,
            "stats": {"files": 0, "additions": 0, "deletions": 0, "complete": True}, "diff": "",
            "truncated": False, "scrubbed": 0}
