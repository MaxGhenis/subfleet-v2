"""Daemon-owned gate state; the CLI is a client of jobs and typed actions.

Every durable transition is journaled in events. v1-shaped files are atomic
projections of that journal, so a file publication failure is recoverable.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path

from ..policy import RETIRED_MODELS, PolicyError, resolve_model
from ..store import utc_now
from ..protocol import SubmitArgs, GateStartArgs, GateContinueArgs, coerce_args, ProtocolError
from .certificate import certificate, load_state, private_dir, write_bytes, write_json
from .errors import GateError
from .revision import (assert_expected, assert_optional_expected, expected_revision,
                       fingerprint, plan, revision)
from .round import prepare
from .verdict import parse_verdict, validate_attestation

_INIT_LOCK = threading.Lock()
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TERMINAL = {"succeeded", "failed", "cancelled", "lost"}
#: The peers a gate dispatches: Astra for a Claude main, Opus for a Codex main.
#: `sol` and `fable` stay accepted as retired spellings of their successors (C-17.2).
PEERS = ("astra", "opus")
CLAUDE_PEERS = frozenset({"opus"})


def current_peer(peer: str) -> str:
    """The peer a new round dispatches: a retired peer's successor, else the peer itself."""
    return RETIRED_MODELS.get(peer, peer)


def read_context(path: str | None, label: str) -> str:
    if not path:
        return ""
    try:
        body = Path(path).expanduser().read_bytes()
        if len(body) > 65536:
            raise GateError(f"{label} exceeds 65536 bytes")
        return body.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise GateError(f"cannot read {label}: {exc}") from exc


def routing(args, peer: str) -> tuple[str | None, tuple[str, ...]]:
    account = getattr(args, "peer_account", None)
    exclusions = tuple(dict.fromkeys(getattr(args, "exclude_account", None) or []))
    if (account is not None or exclusions) and current_peer(peer) not in CLAUDE_PEERS:
        raise GateError("--peer-account and --exclude-account require a Claude peer (opus)")
    if any(not isinstance(x, str) or not x.strip() for x in (*exclusions, *([account] if account is not None else []))):
        raise GateError("peer account routing requires nonempty account names")
    if account is not None and account.casefold() in {x.casefold() for x in exclusions}:
        raise GateError("--peer-account cannot also appear in --exclude-account")
    return account, exclusions


def round_limit(value: int | None, policy: dict, current: int | None = None) -> int:
    cap = int(policy.get("caps", {}).get("gate_max_rounds", 4))
    if cap < 1:
        raise GateError("policy gate_max_rounds must be positive")
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
        raise GateError("--max-rounds must be nonnegative")
    # v1's 0 spelling still parses; the permanent policy cap always applies.
    requested = current if value is None else value
    return min(requested, cap) if requested else cap


def capture(state: dict, *, runner=subprocess.run) -> tuple[dict, bytes | None]:
    from .merge import capture_pr, verify_pr_workspace
    locator = state["locator"]
    if state["kind"] == "plan":
        subject, body = plan(Path(locator["path"]))
        if subject["path"] != locator["path"]:
            raise GateError("plan path now resolves to a different file", 4)
        return subject, body
    subject = capture_pr(str(locator["number"]), cwd=Path(state["workdir"]),
                         repository=locator["repository"], runner=runner)
    if subject["repository"] != locator["repository"] or subject["number"] != locator["number"]:
        raise GateError("PR locator resolved to a different pull request", 4)
    if subject["state"] != "MERGED":
        verify_pr_workspace(Path(state["workdir"]), subject, runner=runner)
    return subject, None


def preview(args, root: Path, *, runner=subprocess.run, policy: dict | None = None) -> dict:
    """C-19.1: read files/remote metadata only, without a daemon or state mutation."""
    if args.gate_command == "continue":
        if not _ID.fullmatch(args.gate_id):
            raise GateError("invalid gate id")
        state = load_state(root / "gates" / args.gate_id)
        subject, _ = capture(state, runner=runner)
        peer = current_peer(state["peer"])
    else:
        peer = current_peer(args.peer)
        cwd = Path(args.workdir or Path.cwd()).expanduser().resolve()
        if args.gate_command == "plan":
            source = Path(args.target).expanduser()
            subject, _ = plan(source if source.is_absolute() else cwd / source)
        else:
            from .merge import capture_pr, verify_pr_workspace
            subject = capture_pr(args.target, cwd=cwd, runner=runner)
            verify_pr_workspace(cwd, subject, runner=runner)
        state = {"kind": args.gate_command, "on_agreement": args.on_agreement,
                 "rounds": [], "workdir": str(cwd)}
    account, exclusions = routing(args, peer)
    rev = revision(subject)
    return {"gate_id": state.get("id"), "kind": state["kind"], "peer": peer,
            "revision": rev, "fingerprint": fingerprint(rev),
            "on_agreement": state["on_agreement"], "status": state.get("status"),
            "next_round": len(state["rounds"]) + 1,
            "max_rounds": round_limit(args.max_rounds, policy or {}, state.get("max_rounds")),
            "peer_account": account, "exclude_accounts": list(exclusions), "dry_run": True}


class GateService:
    def __init__(self, daemon):
        self.daemon, self.store, self.root = daemon, daemon.store, daemon.root
        self.runner = getattr(daemon, "gate_runner", subprocess.run)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_lock = threading.Lock()

    def _lock(self, gate_id):
        if not isinstance(gate_id, str) or not _ID.fullmatch(gate_id):
            raise GateError("invalid gate id")
        with self._locks_lock:
            return self._locks.setdefault(gate_id, threading.RLock())

    def _directory(self, state):
        return self.root / "gates" / state["id"]

    def _load(self, gate_id):
        # The committed journal wins over a partially published file projection.
        rows = self.store.query("SELECT data_json FROM events WHERE kind='gate.state' ORDER BY event_id DESC")
        for row in rows:
            payload = json.loads(row["data_json"])
            if payload.get("gate_id") == gate_id and "state" in payload:
                return payload["state"]
        raise GateError(f"unknown gate: {gate_id}")

    def _journal(self, state, transition):
        state["updated_at"] = utc_now()
        state["version"] = state.get("version", 0) + 1
        self.store.add_event("gate.state", data={"gate_id": state["id"],
                             "transition": transition, "state": state})

    def _project(self, state):
        directory = self._directory(state)
        private_dir(directory)
        write_json(directory / "gate.json", state)
        if state.get("certificate_content"):
            write_json(directory / "certificate.json", state["certificate_content"])
        elif (directory / "certificate.json").exists():
            (directory / "certificate.json").unlink()
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _save(self, state, transition):
        self._journal(state, transition)
        self._project(state)

    @staticmethod
    def _result(state, code=None, message=None):
        if code is None:
            code = {"completed": 0, "changes_requested": 3, "blocked": 4,
                    "action_failed": 5, "action_queued": 5}.get(state["status"])
        last = (state.get("rounds") or [{}])[-1]
        return {"gate_id": state["id"], "status": state["status"], "code": code,
                "round": len(state.get("rounds", [])), "subject": state.get("subject"),
                "action": state.get("action"), "job_id": last.get("peer_run_id"),
                "message": message or state.get("blocker") or last.get("error")}

    def start(self, args):
        from .merge import capture_pr, verify_pr_workspace
        peer = current_peer(args.peer)
        if peer not in PEERS:
            raise GateError("--peer must be astra or opus (sol and fable are retired)")
        account, exclusions = routing(args, peer)
        limit = round_limit(args.max_rounds, self.daemon.policy)
        if not args.main_approve:
            raise GateError("--main-approve is required; approval cannot be inferred from invocation")
        cwd = Path(args.workdir or Path.cwd()).expanduser().resolve()
        if not cwd.is_dir():
            raise GateError(f"workdir is not a directory: {cwd}")
        if args.gate_command == "plan":
            source = Path(args.target).expanduser()
            subject, _ = plan(source if source.is_absolute() else cwd / source)
            locator = {"path": subject["path"]}
            if args.on_agreement != "proceed":
                raise GateError("plan gates support only proceed")
        elif args.gate_command == "pr":
            subject = capture_pr(args.target, cwd=cwd, runner=self.runner)
            verify_pr_workspace(cwd, subject, runner=self.runner)
            locator = {"repository": subject["repository"], "number": subject["number"]}
            if args.on_agreement not in {"proceed", "merge"} or args.merge_method not in {"merge", "squash"}:
                raise GateError("PR actions support proceed or merge, with merge or squash")
        else:
            raise GateError("choose gate pr or plan")
        expected = expected_revision(args, subject)
        assert_expected(revision(subject), expected)
        main_model = getattr(args, "main_model", None)
        peer_family = self.daemon.policy["models"][peer]["provider"]
        if main_model:
            # A retired name or an exact id still names its family (C-11.1).
            try:
                model = self.daemon.policy["models"][resolve_model(self.daemon.policy, main_model, note=False)]
            except PolicyError:
                raise GateError("unknown --main-model") from None
            main_family = model["provider"]
            if main_family == peer_family:
                raise GateError("main and peer must be different model families")
        else:
            # As in v1, peer selection is the caller's complementary-family attestation.
            main_family = "claude" if peer_family == "codex" else "codex"
        state = {"schema_version": 1, "id": utc_now().replace("-", "").replace(":", "").replace("T", "-").rstrip("Z") + f"-{args.gate_command}-{uuid.uuid4().hex[:8]}",
                 "created_at": utc_now(), "updated_at": utc_now(), "status": "ready",
                 "kind": args.gate_command, "locator": locator, "subject": subject,
                 "workdir": str(cwd), "peer": peer, "main_family": main_family,
                 "on_agreement": args.on_agreement, "merge_method": args.merge_method,
                 "max_rounds": limit, "brief": read_context(args.brief, "brief"),
                 "rounds": [], "action": None}
        with self._lock(state["id"]):
            self._save(state, "created")
            return self._reserve(state, expected, "", account, exclusions)

    def _reserve(self, state, expected, response, account, exclusions):
        from .merge import pr_patch
        subject, body = capture(state, runner=self.runner)
        assert_expected(revision(subject), expected)
        rounds = state["rounds"]
        if len(rounds) >= state["max_rounds"]:
            state.update(status="blocked", blocker=self._cap_message(state))
            self._save(state, "round-cap")
            return self._result(state)
        prior = rounds[-1] if rounds else {}
        if prior.get("status") == "changes_requested" and prior.get("revision") == expected and not response.strip():
            raise GateError("artifact is unchanged after changes requested; change it or pass --response FILE", 3)
        if current_peer(state["peer"]) != state["peer"]:
            # A gate opened before its peer was retired continues on the successor,
            # which is of the same family; its earlier rounds keep the peer they
            # ran on, and the certificate names the peer of the agreeing round.
            state.update(peer=current_peer(state["peer"]), retired_peer=state["peer"])
            self._save(state, "peer-retired")
        stamp = utc_now()
        record = {"number": len(rounds) + 1, "attempt_id": uuid.uuid4().hex,
                  "started_at": stamp, "finished_at": None, "revision": expected,
                  "main_approval": {"approved": True, "at": stamp, "expected_revision": expected},
                  "peer": state["peer"], "peer_returncode": None, "peer_run_id": None,
                  "verdict": None, "status": "reviewing", "error": None,
                  "requested_model": self.daemon.policy["models"][state["peer"]]["id"],
                  "peer_account": account, "exclude_accounts": list(exclusions)}
        if body is None:
            body = (pr_patch(Path(state["workdir"]), expected, runner=self.runner) + "\n").encode()
        spec = prepare(self.root, state, record, body, response=response,
                       prior=prior.get("verdict"), peer_account=account, exclusions=exclusions)
        record["submit_args"] = dataclasses.asdict(spec)
        if state.get("certificate_content"):
            prior["certificate"] = state.pop("certificate_content")
            state.pop("certificate", None)
        state.update(status="reviewing", subject=subject)
        state.pop("blocker", None)
        rounds.append(record)
        self._save(state, "round-prepared")
        return self._submit(state)

    def _submit(self, state):
        record = state["rounds"][-1]
        try:
            job = self.daemon.submit(SubmitArgs(**record["submit_args"]))
        except Exception as exc:
            # Durable prepared inputs allow safe inspection; no provider output counts.
            record.update(status="blocked", error=f"peer submission failed: {exc}", finished_at=utc_now())
            state.update(status="blocked", blocker=record["error"])
            self._save(state, "round-submit-failed")
            return self._result(state)
        record["peer_run_id"] = job["job_id"]
        self._save(state, "round-submitted")
        return self._result(state)

    @staticmethod
    def _cap_message(state):
        last = (state.get("rounds") or [{}])[-1]
        return (f"maximum of {state['max_rounds']} peer rounds reached; last verdict: "
                f"{last.get('verdict', {}).get('verdict') if last.get('verdict') else 'not a verdict'}; "
                f"{last.get('error') or (last.get('verdict') or {}).get('summary', '')}")

    def _complete(self, state):
        record = state["rounds"][-1]
        if state["status"] != "agreed" or record["status"] != "approve":
            raise GateError("gate has no latest consensus approval", 4)
        # MergeActions checks the revision before a new submission and recovers
        # existing results by operation key. A landed merge may have advanced
        # the base while the gate's result projection was interrupted.
        if state["on_agreement"] == "proceed":
            current, _ = capture(state, runner=self.runner)
            if revision(current) != record["revision"]:
                state.update(status="blocked", blocker="artifact revision changed before agreement completion")
                self._save(state, "agreement-revision-changed")
                return self._result(state)
        if not state.get("certificate_content"):
            state["certificate_content"] = certificate(state, record, issued_at=utc_now())
            state["certificate"] = str(self._directory(state) / "certificate.json")
            self._save(state, "certificate-issued")
        if state["on_agreement"] == "proceed":
            state.update(status="completed", action={"status": "authorized", "type": "proceed", "at": utc_now()})
            self._save(state, "completed")
            return self._result(state)
        from .merge import MergeActions
        result = MergeActions(self.store, runner=self.runner).execute(state, record)
        return self._action_result(state, result)

    def _action_result(self, state, result):
        state.update(status=result["status"], action=result["action"])
        state["action"]["action_id"] = result["action_id"]
        if result.get("message"):
            state["blocker"] = result["message"]
        self._save(state, "merge-result")
        return self._result(state, result["code"], result.get("message"))

    def poll(self, gate_id):
        with self._lock(gate_id):
            state = self._load(gate_id)
            if state["status"] == "agreed":
                return self._complete(state)
            if state["status"] == "action_attempting":
                from .merge import MergeActions
                result = MergeActions(self.store, runner=self.runner).reconcile(state["action"]["action_id"])
                return self._action_result(state, result)
            if state["status"] != "reviewing":
                self._project(state)
                return self._result(state)
            record = state["rounds"][-1]
            if not record.get("peer_run_id"):
                return self._submit(state)
            job = self.store.get_job(record["peer_run_id"])
            if job and job["state"] not in _TERMINAL:
                return self._result(state)
            return self._consume(state, job)

    def _consume(self, state, job):
        record = state["rounds"][-1]
        expected = record["revision"]
        error, verdict, retry = None, None, False
        attempt = None  # bound only once the peer job is accepted
        try:
            subject, _ = capture(state, runner=self.runner)
            if revision(subject) != expected:
                raise GateError("artifact revision changed while the peer was reviewing", 4)
            state["subject"] = subject
            if not job or job["state"] != "succeeded" or job["rc"] != 0:
                raise GateError(f"peer dispatch exited {job.get('rc') if job else 'without a job'}", 4)
            attempt = self.store.get_attempt(job["accepted_attempt_id"])
            if not attempt or attempt["model_requested"] != record["requested_model"]:
                raise GateError("peer attempt requested a different model", 4)
            evidence = json.loads(attempt.get("evidence_json") or "{}")
            output = Path(record["peer_output"])
            downgrade = next((str(p) for p in (output.with_suffix(".DOWNGRADED"), output.parent / "DOWNGRADED") if p.exists()), None)
            def downgrade_record(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if "downgrad" in key.lower() and item is not None and item is not False:
                            return {key: item}
                        found = downgrade_record(item)
                        if found is not None:
                            return found
                elif isinstance(value, list):
                    for item in value:
                        found = downgrade_record(item)
                        if found is not None:
                            return found
                return None
            if downgrade is None:
                downgrade = downgrade_record(evidence)
            try:
                validate_attestation(attempt["attestation"], record["requested_model"],
                                     served_model=attempt["model_served"], downgrade=downgrade)
            except GateError:
                retry = True
                raise
            artifacts = self.store.list_artifacts(attempt["attempt_id"])
            artifact = next((r for r in artifacts if r["role"] == "deliverable"), None)
            if not artifact:
                raise GateError("peer has no accepted deliverable", 4)
            body = Path(artifact["path"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != artifact["sha256"]:
                raise GateError("peer deliverable changed after acceptance", 4)
            verdict = parse_verdict(body.decode(), expected)
        except (GateError, OSError, UnicodeError) as exc:
            error = str(exc)
        holder = f"gate-round:{record['peer_run_id']}"
        with self.store.transaction("gate.round-consumed", job_id=record["peer_run_id"], data={"gate_id": state["id"]}) as tx:
            lease = self.store.one("SELECT holder FROM leases WHERE lease_key=?", (record["round_lease"],))
            if not lease or lease["holder"] != holder:
                error, verdict, retry = "gate review lease is no longer held; abandoned output is not a verdict", None, False
                self.store.add_event("gate.round-discarded", data={"gate_id": state["id"], "reason": error})
            record.update(finished_at=utc_now(), peer_returncode=job.get("rc") if job else None,
                          verdict=verdict, status="blocked" if error else verdict["verdict"], error=error,
                          # C-23.43: the round record notes the attestation a verdict was
                          # accepted under (an unattested Codex round is legitimate).
                          peer_attestation=attempt.get("attestation") if attempt else None,
                          peer_model_served=attempt.get("model_served") if attempt else None)
            if error or (verdict and verdict["verdict"] == "blocked"):
                state.update(status="blocked", blocker=error or verdict["summary"])
            elif verdict["verdict"] == "approve":
                state.update(status="agreed")
            else:
                state.update(status="changes_requested")
            if state["status"] != "agreed" and len(state["rounds"]) >= state["max_rounds"]:
                state.update(status="blocked", blocker=self._cap_message(state))
                retry = False
            self._journal(state, "round-finished")
            tx.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (record["round_lease"], holder))
        self._project(state)
        if verdict:
            write_json(Path(record["peer_output"]).parent / "verdict.json", verdict)
        if retry:
            return self._reserve(state, expected, "", record.get("peer_account"), tuple(record.get("exclude_accounts", [])))
        return self._complete(state) if state["status"] == "agreed" else self._result(state)

    def continue_gate(self, args):
        with self._lock(args.gate_id):
            state = self._load(args.gate_id)
            account, exclusions = routing(args, state["peer"])
            approved = state["rounds"][-1]["revision"] if state["rounds"] else None
            if state["status"] == "agreed" and state["on_agreement"] == "merge":
                op_key = f"{approved['repository'].casefold()}:{approved['number']}:{approved['head_sha'].lower()}"
                if self.store.one("SELECT action_id FROM actions WHERE op_key=?", (op_key,)):
                    # The holder may have landed the merge before gate-state
                    # publication. Recover that action against its approval.
                    assert_optional_expected(args, state["subject"], approved)
                    return self._complete(state)
            if state["status"] == "reviewing":
                job = self.store.get_job(state["rounds"][-1].get("peer_run_id"))
                if job and job["state"] not in _TERMINAL:
                    return self._result(state, 4, "another peer review is already running; no duplicate launched")
                current, _ = capture(state, runner=self.runner)
                if args.main_approve:
                    assert_expected(revision(current), expected_revision(args, current))
                else:
                    assert_optional_expected(args, current, state["rounds"][-1]["revision"])
                return self.poll(args.gate_id)
            subject, _ = capture(state, runner=self.runner)
            if state["status"] == "completed":
                assert_optional_expected(args, subject, approved)
                if state["on_agreement"] == "merge":
                    from .merge import verify_landing
                    blocker = verify_landing(state, subject, approved, runner=self.runner)
                    if blocker:
                        raise GateError(blocker, 4)
                else:
                    assert_expected(revision(subject), approved)
                return self._result(state, 0, "gate already completed; no action repeated")
            action_id = (state.get("action") or {}).get("action_id")
            if action_id:
                from .merge import MergeActions
                expected = expected_revision(args, subject) if args.main_approve else approved
                if expected == approved:
                    assert_optional_expected(args, subject, approved)
                    return self._action_result(state, MergeActions(self.store, runner=self.runner).reconcile(action_id))
                assert_expected(revision(subject), expected)
                settled = MergeActions(self.store, runner=self.runner).reconcile(action_id)
                if settled["action"]["status"] not in {"blocked", "failed"}:
                    # An unknown, queued, or mismatched landing never authorizes another action.
                    return self._action_result(state, settled)
                state["rounds"][-1]["action"] = state["action"]
                state.update(action=None, status="blocked")
            if not args.main_approve:
                raise GateError("--main-approve is required for every fresh round")
            expected = expected_revision(args, subject)
            assert_expected(revision(subject), expected)
            if state["status"] == "agreed" and approved == expected:
                return self._complete(state)
            state["max_rounds"] = round_limit(args.max_rounds, self.daemon.policy, state["max_rounds"])
            return self._reserve(state, expected, read_context(args.response, "main response"), account, exclusions)


def dispatch(daemon, op: str, args: dict) -> dict:
    recovery = getattr(daemon, "_recovery_complete", None)
    if recovery is not None and not recovery.is_set():
        return {"code": 1, "status": "error", "message": "daemon recovery is in progress; retry the gate command"}
    with _INIT_LOCK:
        if not hasattr(daemon, "_gate_service"):
            daemon._gate_service = GateService(daemon)
    service = daemon._gate_service
    try:
        if op == "gate.start":
            request = coerce_args(GateStartArgs, args)
            if request.dry_run:
                return {**preview(request, service.root, runner=service.runner, policy=daemon.policy), "code": 0}
            return service.start(request)
        if op == "gate.poll":
            return service.poll(args["gate_id"])
        if op == "gate.continue":
            request = coerce_args(GateContinueArgs, args)
            if request.dry_run:
                return {**preview(request, service.root, runner=service.runner, policy=daemon.policy), "code": 0}
            return service.continue_gate(request)
        raise GateError("unknown gate operation")
    except (GateError, ProtocolError) as exc:
        return {"code": int(exc.code), "status": "error", "message": str(exc)}
