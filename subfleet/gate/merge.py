"""Durable, fingerprint-bound GitHub merge actions (C-19, C-23.11–13).

All commands use the injectable runner. Terminal action rows are immutable;
remote reads settle an unknown outcome with an additive reconciliation event.
"""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from ..actions import Actions
from ..store import utc_now
from .errors import GateError

RunCommand = Callable[..., subprocess.CompletedProcess[str]]
MERGE_METHODS = frozenset({"merge", "squash"})
SUCCESS_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_OID = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_PR_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)$")


def run_command(command: list[str], *, cwd: Path, runner: RunCommand = subprocess.run):
    """C-23.11: one bounded command; never retry a remote submission."""
    return runner(command, cwd=str(cwd), capture_output=True, text=True,
                  stdin=subprocess.DEVNULL, timeout=30)


def capture_pr(target: str, *, cwd: Path, repository: str | None = None,
               runner: RunCommand = subprocess.run) -> dict:
    """C-23.8, C-23.11: normalize a read without inferring caller approval."""
    command = ["gh", "pr", "view", str(target)]
    if repository:
        command += ["--repo", repository]
    command += ["--json", "url,number,state,isDraft,headRefOid,baseRefOid,mergeable,"
                "mergeStateStatus,statusCheckRollup,mergeCommit"]
    try:
        completed = run_command(command, cwd=cwd, runner=runner)
        if completed.returncode:
            raise GateError(f"cannot resolve PR {target}: " +
                            (completed.stderr or completed.stdout or "gh exited nonzero").strip(), 1)
        data = json.loads(completed.stdout)
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        match = _PR_URL.fullmatch(str(data.get("url") or ""))
        if match is None:
            raise ValueError("missing canonical GitHub URL")
        repo, number = match.group(1), int(match.group(2))
        if type(data.get("number")) is not int or data["number"] != number:
            raise ValueError("PR number differs from canonical GitHub URL")
        if repository and repo.casefold() != repository.casefold():
            raise ValueError("PR repository differs from requested repository")
        if str(target).isdigit() and int(target) != number:
            raise ValueError("PR number differs from requested PR")
        for name in ("headRefOid", "baseRefOid"):
            if not isinstance(data.get(name), str) or not _OID.fullmatch(data[name]):
                raise ValueError("missing full head or base commit OID")
        if type(data.get("isDraft")) is not bool:
            raise ValueError("missing draft status")
        merged = data.get("mergeCommit")
        if merged is not None and not isinstance(merged, dict):
            raise ValueError("invalid merge commit")
        return {"kind": "pr", "repository": repo, "number": number, "url": data["url"],
                "head_sha": data["headRefOid"].lower(), "base_sha": data["baseRefOid"].lower(),
                "state": str(data.get("state") or "UNKNOWN").upper(), "is_draft": data["isDraft"],
                "mergeable": str(data.get("mergeable") or "UNKNOWN").upper(),
                "merge_state_status": str(data.get("mergeStateStatus") or "UNKNOWN").upper(),
                "checks": data.get("statusCheckRollup"), "merge_commit": (merged or {}).get("oid")}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        if isinstance(exc, GateError):
            raise
        raise GateError(f"cannot read PR metadata: {exc}", 1) from exc


def git_output(cwd: Path, arguments: list[str], *, runner: RunCommand = subprocess.run) -> str:
    """C-23.8: inspect only the approved local checkout through the runner."""
    try:
        completed = run_command(["git", *arguments], cwd=cwd, runner=runner)
        if completed.returncode:
            raise GateError("git inspection failed: " +
                            (completed.stderr or completed.stdout or "nonzero exit").strip(), 1)
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"cannot inspect PR checkout: {exc}", 1) from exc


def verify_pr_workspace(cwd: Path, subject: dict, *, runner: RunCommand = subprocess.run) -> None:
    """C-23.8, C-23.11: require the exact head and a clean local checkout."""
    if git_output(cwd, ["rev-parse", "--verify", "HEAD"], runner=runner) != subject["head_sha"]:
        raise GateError("PR gate requires a checkout at the exact PR head", 4)
    if git_output(cwd, ["status", "--porcelain=v1", "--untracked-files=all"], runner=runner):
        raise GateError("PR gate requires a clean worktree at the approved head", 4)


def pr_patch(cwd: Path, revision: dict, *, runner: RunCommand = subprocess.run) -> str:
    """C-23.8: capture the immutable approved comparison for the peer bundle."""
    return git_output(cwd, ["diff", "--no-ext-diff", "--no-textconv", "--find-renames",
                           "--find-copies", "--binary",
                           f"{revision['base_sha']}...{revision['head_sha']}", "--"], runner=runner)


def checks_green(checks: Any) -> tuple[bool, str]:
    """C-23.11: preserve v1's strict terminal-green check rollup semantics."""
    if not isinstance(checks, list) or not checks:
        return False, "the PR has no reported CI checks"
    for item in checks:
        if not isinstance(item, dict):
            return False, "the PR has malformed CI metadata"
        if item.get("__typename") == "CheckRun" or "conclusion" in item:
            status = str(item.get("status") or "").upper()
            conclusion = str(item.get("conclusion") or "").upper()
            if status != "COMPLETED" or conclusion not in SUCCESS_CONCLUSIONS:
                return False, f"CI check {item.get('name') or 'check'!r} is {status or '?'} / {conclusion or '?'}"
        elif str(item.get("state") or "").upper() != "SUCCESS":
            return False, f"CI status {item.get('context') or 'status'!r} is {item.get('state') or '?'}"
    return True, "all reported checks are terminal and green"


def merge_blocker(subject: dict, expected: dict) -> str | None:
    """C-23.11: the immediately preceding PR read must meet every precondition."""
    if subject["repository"] != expected["repository"] or subject["number"] != expected["number"]:
        return "PR identity changed after approval"
    if subject["state"] != "OPEN":
        return f"PR state is {subject['state']}, not OPEN"
    if subject["is_draft"]:
        return "PR is still a draft"
    if subject["head_sha"] != expected["head_sha"]:
        return "PR head changed after approval"
    if subject["base_sha"] != expected["base_sha"]:
        return "PR base changed after approval"
    if subject["mergeable"] != "MERGEABLE":
        return f"PR mergeability is {subject['mergeable']}, not MERGEABLE"
    if subject["merge_state_status"] != "CLEAN":
        return f"PR mergeStateStatus is {subject['merge_state_status']}, not CLEAN"
    green, reason = checks_green(subject.get("checks"))
    return None if green else reason


def _landing(state: dict, subject: dict, expected: dict, *, runner: RunCommand) -> tuple[str, str | None]:
    if (subject["repository"] != expected["repository"] or subject["number"] != expected["number"]
            or subject["head_sha"] != expected["head_sha"]):
        return "mismatch", "merged PR does not have the approved identity and head"
    method = state["merge_method"]
    if method not in MERGE_METHODS:
        return "mismatch", "the landing method cannot be verified by this gate"
    merge_commit = subject.get("merge_commit")
    if not isinstance(merge_commit, str) or not _OID.fullmatch(merge_commit):
        return "unknown", "merged PR has no verifiable merge commit"
    try:
        completed = run_command(["gh", "api", f"repos/{expected['repository']}/git/commits/{merge_commit}"],
                                cwd=Path(state["workdir"]), runner=runner)
        if completed.returncode:
            return "unknown", "could not read the immutable merge commit's parents"
        commit = json.loads(completed.stdout)
        if not isinstance(commit, dict) or not isinstance(commit.get("parents"), list):
            return "unknown", "could not verify the immutable merge commit's parents"
        if commit.get("sha") != merge_commit:
            return "mismatch", "merge commit lookup returned a different commit"
        parents = [parent["sha"] for parent in commit["parents"]]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return "unknown", "could not verify the immutable merge commit's parents"
    wanted = [expected["base_sha"]] + ([expected["head_sha"]] if method == "merge" else [])
    if parents != wanted:
        return "mismatch", "PR was merged, but its landing parents differ from the approved revision"
    return "confirmed", None


def verify_landing(state: dict, subject: dict, revision: dict, *, runner: RunCommand = subprocess.run) -> str | None:
    """C-23.12: return a blocker using immutable commit parents, never the base tip."""
    if subject.get("state") != "MERGED":
        return "completed merge is not confirmed by current PR state"
    return _landing(state, subject, revision, runner=runner)[1]


def merge_pending(state: dict, *, runner: RunCommand = subprocess.run) -> bool | None:
    """C-19.1: read queue membership before settling an open PR; unknown cannot retry."""
    locator = state["locator"]
    owner, name = locator["repository"].split("/", 1)
    query = ("query($owner:String!,$name:String!,$number:Int!) { repository(owner:$owner,name:$name) "
             "{ pullRequest(number:$number) { number url state isInMergeQueue autoMergeRequest { enabledAt } } } }")
    try:
        completed = run_command(["gh", "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
                                "-f", f"name={name}", "-F", f"number={locator['number']}"],
                                cwd=Path(state["workdir"]), runner=runner)
        if completed.returncode:
            return None
        value = json.loads(completed.stdout)
        if value.get("errors"):
            return None
        pr = value["data"]["repository"]["pullRequest"]
        if (pr["number"] != locator["number"] or
                pr["url"] != f"https://github.com/{locator['repository']}/pull/{locator['number']}" or
                pr["state"] != "OPEN" or type(pr["isInMergeQueue"]) is not bool or
                (pr["autoMergeRequest"] is not None and not isinstance(pr["autoMergeRequest"], dict))):
            return None
        return pr["isInMergeQueue"] or pr["autoMergeRequest"] is not None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return None


class MergeActions:
    """C-19.1, C-23.13: one durable holder and one remote attempt per operation key."""

    def __init__(self, store, *, runner: RunCommand = subprocess.run):
        self.store, self.runner = store, runner
        self.actions = Actions(store)

    def _response(self, action: dict, result: dict | None = None) -> dict:
        result = result or json.loads(action.get("result_json") or "{}")
        status = result.get("status", "attempting" if action["state"] in {"pending", "executing"} else "unknown")
        gate_status = ("completed" if status == "merged" else "blocked" if status == "blocked"
                       else "action_queued" if status == "queued" else "action_attempting" if status == "attempting"
                       else "action_failed")
        return {"code": result.get("code", 5), "status": gate_status,
                "action_id": action["action_id"], "action_state": action["state"],
                "action": {**result, "status": status, "action_id": action["action_id"]}}

    def _publish(self, action_id: str, holder: str, state: str, result: dict) -> dict:
        if not self.actions.publish(action_id, holder, state, result, now=utc_now()):
            action = self.store.get_action(action_id)
            return self._response(action, {"status": "unknown", "code": 5,
                                         "reason": "merge action result discarded: holder changed"})
        return self._response(self.store.get_action(action_id))

    def _observe(self, state: dict, expected: dict) -> tuple[str, dict]:
        try:
            post = capture_pr(str(expected["number"]), cwd=Path(state["workdir"]),
                              repository=expected["repository"], runner=self.runner)
        except GateError as exc:
            return "unknown", {"status": "unknown", "code": 5, "reason": f"could not reconcile PR: {exc}"}
        if post["state"] == "MERGED":
            outcome, reason = _landing(state, post, expected, runner=self.runner)
            if outcome == "confirmed":
                return "confirmed", {"status": "merged", "code": 0, "postcheck": post}
            return ("failed" if outcome == "mismatch" else "unknown"), {
                "status": "merged_revision_mismatch" if outcome == "mismatch" else "unknown",
                "code": 5, "reason": reason, "postcheck": post}
        if post["state"] == "OPEN":
            if post["head_sha"] != expected["head_sha"] or post["base_sha"] != expected["base_sha"]:
                return "failed", {"status": "blocked", "code": 4,
                                  "reason": "PR head or base changed between preflight and merge", "postcheck": post}
            pending = merge_pending(state, runner=self.runner)
            if pending is True:
                return "unknown", {"status": "queued", "code": 5, "postcheck": post,
                                   "reason": "GitHub confirms a pending queue/auto-merge request"}
            if pending is None:
                return "unknown", {"status": "queue_unknown", "code": 5, "postcheck": post,
                                   "reason": "PR remains open; queue status could not be verified"}
        return "failed", {"status": "failed", "code": 5, "postcheck": post,
                          "reason": f"PR remains {post['state']}; no pending merge was confirmed"}

    def execute(self, state: dict, round_state: dict, *, holder: str | None = None,
                dry_run: bool = False) -> dict:
        """C-19.1, C-23.8–13: persist intent, claim, preflight, submit, verify once."""
        expected = round_state.get("revision") or {}
        approval = round_state.get("main_approval") or {}
        verdict = round_state.get("verdict") or {}
        if (state.get("kind") != "pr" or state.get("on_agreement") != "merge" or
                state.get("merge_method") not in MERGE_METHODS):
            raise GateError("merge actions require a PR gate and --merge-method merge or squash")
        if (round_state.get("status") != "approve" or verdict.get("verdict") != "approve" or
                verdict.get("artifact_revision") != expected or verdict.get("findings") != [] or
                verdict.get("notes") != [] or approval.get("approved") is not True or
                approval.get("expected_revision") != expected):
            raise GateError("merge requires explicit main and peer approval of the same fingerprint", 4)
        if (expected.get("kind") != "pr" or expected.get("repository") != state["locator"]["repository"] or
                expected.get("number") != state["locator"]["number"] or
                any(not isinstance(expected.get(key), str) or not _OID.fullmatch(expected[key])
                    for key in ("head_sha", "base_sha"))):
            raise GateError("merge approval lacks a valid exact PR revision", 4)
        op_key = f"{expected['repository'].casefold()}:{expected['number']}:{expected['head_sha'].lower()}"
        if dry_run:
            return {"code": 0, "status": "dry-run", "op_key": op_key, "revision": expected}
        action_id, holder = str(uuid.uuid4()), holder or str(uuid.uuid4())
        request = {"gate_id": state["id"], "approved_revision": expected,
                   "workdir": state["workdir"], "merge_method": state["merge_method"],
                   "locator": state["locator"], "round_attempt_id": round_state.get("attempt_id")}
        with self.store.transaction("action.pending", data={"action_id": action_id, "kind": "merge"}):
            existing = self.store.one("SELECT * FROM actions WHERE op_key=?", (op_key,))
            if existing is None:
                self.store.add_action(action_id=action_id, kind="merge", op_key=op_key,
                                      subject=f"{expected['repository']}#{expected['number']}",
                                      request_json=json.dumps(request, sort_keys=True))
        if existing is not None:
            return self.reconcile(existing["action_id"]) if existing["state"] == "unknown" else self._response(existing)
        if not self.actions.claim(action_id, holder, now=utc_now()):
            return self._response(self.store.get_action(action_id))
        try:
            # Workspace inspection precedes the remote preflight so the PR read
            # is the last external observation before the pinned merge call.
            verify_pr_workspace(Path(state["workdir"]), expected, runner=self.runner)
            current = capture_pr(str(expected["number"]), cwd=Path(state["workdir"]),
                                 repository=expected["repository"], runner=self.runner)
            blocker = merge_blocker(current, expected)
        except GateError as exc:
            blocker = str(exc)
        if blocker:
            return self._publish(action_id, holder, "failed", {"status": "blocked", "code": 4,
                                  "reason": blocker, "approved_revision": expected})
        command = ["gh", "pr", "merge", str(expected["number"]), "--repo", expected["repository"],
                   "--match-head-commit", expected["head_sha"], f"--{state['merge_method']}"]
        held = self.store.get_action(action_id)
        if held["state"] != "executing" or json.loads(held["request_json"]).get("holder") != holder:
            return self._publish(action_id, holder, "failed", {"status": "blocked", "code": 4,
                                                              "reason": "merge action holder changed"})
        dispatch = {"approved_revision": expected, "command": command, "precheck": current}
        try:
            completed = run_command(command, cwd=Path(state["workdir"]), runner=self.runner)
            dispatch.update(returncode=completed.returncode, stdout=(completed.stdout or "")[-4000:],
                            stderr=(completed.stderr or "")[-4000:])
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            # Persist ambiguity immediately. A subsequent read may settle it,
            # but a timeout never causes another submission for this op_key.
            return self._publish(action_id, holder, "unknown", {**dispatch, "status": "unknown", "code": 5,
                                  "dispatch_error": str(exc), "reason": "merge outcome requires remote reconciliation"})
        outcome, result = self._observe(state, expected)
        return self._publish(action_id, holder, outcome, {**dispatch, **result})

    def reconcile(self, action_id: str, *, holder: str | None = None) -> dict:
        """C-19.1, C-23.13: append remote-read settlement, never replace a holder result."""
        action = self.store.get_action(action_id)
        if action is None or action["kind"] != "merge":
            raise GateError("unknown merge action", 2)
        if action["state"] != "unknown":
            return self._response(action)
        request = json.loads(action["request_json"])
        # Continuation reads on behalf of the existing durable owner; it never
        # claims a new attempt or changes the original terminal action row.
        owner = holder if holder is not None else request.get("holder")
        if not owner or request.get("holder") != owner:
            self.store.add_event("action.result-discarded", data={"action_id": action_id,
                                 "holder": owner, "offered_state": "reconciled"})
            return self._response(action, {"status": "unknown", "code": 5,
                                          "reason": "merge reconciliation holder changed"})
        for event in self.store.query("SELECT data_json FROM events WHERE kind='action.reconciled' ORDER BY event_id"):
            settled = json.loads(event["data_json"])
            if settled.get("action_id") == action_id and isinstance(settled.get("result"), dict):
                return self._response(action, settled["result"])
        outcome, observed = self._observe(request, request["approved_revision"])
        result = {**json.loads(action.get("result_json") or "{}"), **observed}
        if outcome != "unknown":
            with self.store.transaction("action.reconciliation", data={"action_id": action_id}):
                current = self.store.get_action(action_id)
                if current["state"] != "unknown" or json.loads(current["request_json"]).get("holder") != owner:
                    self.store.add_event("action.result-discarded", data={"action_id": action_id,
                                         "holder": owner, "offered_state": "reconciled"})
                    return self._response(current, {"status": "unknown", "code": 5,
                                                  "reason": "merge reconciliation holder changed"})
                # Another reader may have settled it while this read was in flight.
                for event in self.store.query("SELECT data_json FROM events WHERE kind='action.reconciled' ORDER BY event_id"):
                    settled = json.loads(event["data_json"])
                    if settled.get("action_id") == action_id and isinstance(settled.get("result"), dict):
                        return self._response(action, settled["result"])
                self.store.add_event("action.reconciled", data={"action_id": action_id,
                                     "original_state": "unknown", "effective_state": outcome,
                                     "holder": owner, "observed_at": utc_now(), "result": result})
        return self._response(action, result)
