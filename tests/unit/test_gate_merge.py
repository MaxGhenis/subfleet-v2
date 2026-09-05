"""Fake-GitHub merge actions: C-19.1 and C-23.8, C-23.11–13."""
from __future__ import annotations

import copy
import json
import subprocess

import pytest

from subfleet.gate.errors import GateError
from subfleet.gate.merge import MergeActions, capture_pr, checks_green, verify_landing
from subfleet.store import Store

HEAD, BASE, LANDING, OTHER = (letter * 40 for letter in "abcd")
REPO = "example/project"
REVISION = {"kind": "pr", "repository": REPO, "number": 42, "head_sha": HEAD, "base_sha": BASE}


@pytest.fixture
def store(tmp_path):
    """C-19.1: every test uses its own local actions database."""
    with Store(tmp_path / "store.sqlite3") as database:
        yield database


def agreement(tmp_path, method="squash"):
    """C-23.8: main and peer both name the caller-attested commit pair."""
    state = {"id": "gate-test", "kind": "pr", "workdir": str(tmp_path), "on_agreement": "merge",
             "merge_method": method, "locator": {"repository": REPO, "number": 42}}
    round_state = {"status": "approve", "revision": copy.deepcopy(REVISION), "attempt_id": "round-owner",
                   "main_approval": {"approved": True, "expected_revision": copy.deepcopy(REVISION)},
                   "verdict": {"verdict": "approve", "artifact_revision": copy.deepcopy(REVISION),
                               "findings": [], "notes": []}}
    return state, round_state


class FakeGh:
    """C-23.11–12: implement only explicitly enumerated local fake commands."""

    def __init__(self, **overrides):
        self.metadata = {"url": f"https://github.com/{REPO}/pull/42", "number": 42,
                         "state": "OPEN", "isDraft": False, "headRefOid": HEAD, "baseRefOid": BASE,
                         "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "mergeCommit": None,
                         "statusCheckRollup": [{"__typename": "CheckRun", "name": "tests",
                                                "status": "COMPLETED", "conclusion": "SUCCESS"}]}
        self.metadata.update(overrides)
        self.commands = []
        self.local_head = HEAD
        self.dirty = ""
        self.timeout = False
        self.queue = False
        self.queue_available = True
        self.view_available = True
        self.parents_available = True
        self.landing_base = BASE
        self.landing_head = HEAD
        self.landing_lookup = LANDING
        self.move_head_on_merge = False
        self.on_merge = None
        self.on_view = None
        self.returncode = 0

    @property
    def merges(self):
        """C-19.1: count actual remote submission attempts."""
        return [command for command in self.commands if command[:3] == ["gh", "pr", "merge"]]

    def landed(self):
        """C-23.12: move the base tip to prove parents carry landing authority."""
        self.metadata.update(state="MERGED", mergeCommit={"oid": LANDING}, baseRefOid=OTHER)

    def __call__(self, command, **kwargs):
        """C-23.11: fail immediately if implementation calls any unexpected tool."""
        self.commands.append(command)
        assert kwargs["timeout"] == 30
        if command[:3] == ["git", "rev-parse", "--verify"]:
            value, rc = self.local_head, 0
        elif command[:2] == ["git", "status"]:
            value, rc = self.dirty, 0
        elif command[:3] == ["gh", "pr", "view"]:
            if self.on_view:
                self.on_view()
            value, rc = json.dumps(self.metadata), 0 if self.view_available else 1
        elif command[:3] == ["gh", "pr", "merge"]:
            assert command[command.index("--match-head-commit") + 1] == HEAD
            assert "--admin" not in command and "--auto" not in command
            if self.on_merge:
                self.on_merge()
            if self.timeout:
                raise subprocess.TimeoutExpired(command, 30)
            if self.move_head_on_merge:
                self.metadata["headRefOid"] = OTHER
                value, rc = "head commit does not match", 1
            else:
                if not self.queue and not self.returncode:
                    self.landed()
                value, rc = "", self.returncode
        elif command[:3] == ["gh", "api", "graphql"]:
            pr = {"number": 42, "url": f"https://github.com/{REPO}/pull/42", "state": "OPEN",
                  "isInMergeQueue": self.queue, "autoMergeRequest": None}
            value = json.dumps({"data": {"repository": {"pullRequest": pr}}})
            rc = 0 if self.queue_available else 1
        elif command == ["gh", "api", f"repos/{REPO}/git/commits/{LANDING}"]:
            parents = [{"sha": self.landing_base}]
            if self.merges and "--merge" in self.merges[-1]:
                parents.append({"sha": self.landing_head})
            value = json.dumps({"sha": self.landing_lookup, "parents": parents})
            rc = 0 if self.parents_available else 1
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, rc, value, "fake failure" if rc else "")


@pytest.mark.parametrize("method", ["merge", "squash"])
def test_merge_intent_and_holder_precede_remote_call_and_pin_head(store, tmp_path, method, monkeypatch):
    """C-19.1, C-23.11–13: persist pending, claim once, and verify immutable parents."""
    runner = FakeGh()
    service = MergeActions(store, runner=runner)
    original_claim = service.actions.claim
    claims = []

    def claim(action_id, holder, *, now):
        action = store.get_action(action_id)
        assert action["state"] == "pending"
        assert not runner.commands
        claims.append(action_id)
        return original_claim(action_id, holder, now=now)

    def remote_read():
        action = store.one("SELECT * FROM actions")
        assert action["state"] == "executing"
        assert json.loads(action["request_json"])["holder"] == "worker-one"

    monkeypatch.setattr(service.actions, "claim", claim)
    runner.on_view = remote_read
    result = service.execute(*agreement(tmp_path, method), holder="worker-one")
    assert result["code"] == 0 and result["status"] == "completed"
    assert result["action"]["status"] == "merged"
    assert result["action_state"] == "confirmed"
    assert len(claims) == 1
    assert runner.merges == [["gh", "pr", "merge", "42", "--repo", REPO,
                              "--match-head-commit", HEAD, f"--{method}"]]
    action = store.get_action(result["action_id"])
    assert action["op_key"] == f"{REPO}:42:{HEAD}"
    assert "action.pending" in {event["kind"] for event in store.list_events()}
    assert service.execute(*agreement(tmp_path, method))["code"] == 0
    assert len(runner.merges) == 1


@pytest.mark.parametrize("override,reason", [
    ({"state": "CLOSED"}, "state"), ({"state": "MERGED"}, "state"),
    ({"isDraft": True}, "draft"), ({"headRefOid": OTHER}, "head changed"),
    ({"baseRefOid": OTHER}, "base changed"), ({"mergeable": "CONFLICTING"}, "mergeability"),
    ({"mergeable": "UNKNOWN"}, "mergeability"), ({"mergeStateStatus": "DIRTY"}, "mergeStateStatus"),
    ({"mergeStateStatus": "BEHIND"}, "mergeStateStatus"), ({"statusCheckRollup": []}, "no reported"),
    ({"statusCheckRollup": None}, "no reported"), ({"statusCheckRollup": [1]}, "malformed"),
    ({"statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}]}, "IN_PROGRESS"),
    ({"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}, "FAILURE"),
    ({"statusCheckRollup": [{"state": "PENDING", "context": "legacy"}]}, "PENDING"),
])
def test_preflight_blocks_every_unsafe_remote_state(store, tmp_path, override, reason):
    """C-23.11: closed, draft, moved, unmergeable, missing, pending, or failed CI blocks at 4."""
    runner = FakeGh(**override)
    result = MergeActions(store, runner=runner).execute(*agreement(tmp_path))
    assert result["code"] == 4 and result["action_state"] == "failed"
    assert result["action"]["status"] == "blocked"
    assert reason in result["action"]["reason"]
    assert result["message"] == result["action"]["reason"]
    assert runner.merges == []


@pytest.mark.parametrize("property,value", [("local_head", OTHER), ("dirty", " M tracked\n?? new\n")])
def test_local_checkout_must_still_be_clean_and_exact(store, tmp_path, property, value):
    """C-23.8, C-23.11: changed or dirty supporting source cannot reach the merge call."""
    runner = FakeGh()
    setattr(runner, property, value)
    result = MergeActions(store, runner=runner).execute(*agreement(tmp_path))
    assert result["code"] == 4 and runner.merges == []


@pytest.mark.parametrize("conclusion", ["SUCCESS", "NEUTRAL", "SKIPPED"])
def test_terminal_check_success_set_matches_v1(conclusion):
    """C-23.11: preserve v1 terminal conclusions and legacy successful statuses."""
    assert checks_green([{"status": "COMPLETED", "conclusion": conclusion}, {"state": "SUCCESS"}])[0]


def test_head_moves_between_preflight_and_merge_and_guard_refuses(store, tmp_path):
    """C-23.8, C-23.11: --match-head-commit blocks the race and reports revision drift as 4."""
    runner = FakeGh()
    runner.move_head_on_merge = True
    service = MergeActions(store, runner=runner)
    result = service.execute(*agreement(tmp_path))
    assert result["code"] == 4 and result["action"]["status"] == "blocked"
    assert len(runner.merges) == 1 and runner.metadata["state"] == "OPEN"
    assert service.execute(*agreement(tmp_path))["code"] == 4
    assert len(runner.merges) == 1


@pytest.mark.parametrize("failure", ["timeout", "postread"])
def test_ambiguous_call_is_unknown_then_read_settles_without_overwrite(store, tmp_path, failure):
    """C-19.1, C-23.13: preserve unknown result and append read reconciliation; never retry."""
    runner = FakeGh()
    runner.timeout = failure == "timeout"
    if failure == "postread":
        runner.on_merge = lambda: setattr(runner, "view_available", False)
    service = MergeActions(store, runner=runner)
    first = service.execute(*agreement(tmp_path))
    assert first["code"] == 5 and first["action_state"] == "unknown"
    action_before = store.get_action(first["action_id"])
    runner.landed()
    runner.view_available = True
    settled = service.reconcile(first["action_id"])
    assert settled["code"] == 0 and settled["action"]["status"] == "merged"
    assert store.get_action(first["action_id"]) == action_before
    events = [json.loads(row["data_json"]) for row in store.query(
        "SELECT data_json FROM events WHERE kind='action.reconciled'")]
    assert any(event.get("effective_state") == "confirmed" for event in events)
    assert len(runner.merges) == 1
    assert service.reconcile(first["action_id"])["code"] == 0
    assert len(runner.merges) == 1


@pytest.mark.parametrize("attribute", ["landing_base", "landing_head", "landing_lookup"])
def test_landing_mismatch_is_recorded_and_never_retried_or_reverted(store, tmp_path, attribute):
    """C-23.12–13: mismatch is terminal, including wrong immutable commit identity or parents."""
    runner = FakeGh()
    setattr(runner, attribute, OTHER)
    service = MergeActions(store, runner=runner)
    state, round_state = agreement(tmp_path, "merge")
    result = service.execute(state, round_state)
    assert result["code"] == 5 and result["action_state"] == "failed"
    assert result["action"]["status"] == "merged_revision_mismatch"
    before = store.get_action(result["action_id"])
    assert service.execute(state, round_state)["code"] == 5
    assert service.reconcile(result["action_id"])["code"] == 5
    assert store.get_action(result["action_id"]) == before
    assert len(runner.merges) == 1
    assert not any("revert" in command for command in runner.commands)


def test_timeout_then_mismatched_landing_settles_as_mismatch(store, tmp_path):
    """C-19.1, C-23.12–13: delayed mismatch remains failed evidence without changing unknown row."""
    runner = FakeGh()
    runner.timeout = True
    service = MergeActions(store, runner=runner)
    first = service.execute(*agreement(tmp_path))
    before = store.get_action(first["action_id"])
    runner.landing_base = OTHER
    runner.landed()
    result = service.reconcile(first["action_id"])
    assert result["code"] == 5 and result["action"]["status"] == "merged_revision_mismatch"
    assert store.get_action(first["action_id"]) == before
    assert service.execute(*agreement(tmp_path))["action"]["status"] == "merged_revision_mismatch"
    assert len(runner.merges) == 1


def test_unavailable_immutable_parents_stay_unknown_until_the_read_succeeds(store, tmp_path):
    """C-19.1, C-23.12: missing verification evidence never certifies a landing."""
    runner = FakeGh()
    runner.parents_available = False
    service = MergeActions(store, runner=runner)
    result = service.execute(*agreement(tmp_path))
    assert result["code"] == 5 and result["action_state"] == "unknown"
    runner.parents_available = True
    assert service.reconcile(result["action_id"])["code"] == 0
    assert len(runner.merges) == 1


@pytest.mark.parametrize("queue_available", [True, False])
def test_pending_or_unknown_queue_membership_never_repeats_a_merge(store, tmp_path, queue_available):
    """C-19.1: queued and unverifiable queue requests remain unknown and cannot dispatch twice."""
    runner = FakeGh()
    runner.queue = True
    runner.queue_available = queue_available
    service = MergeActions(store, runner=runner)
    result = service.execute(*agreement(tmp_path))
    assert result["code"] == 5 and result["action_state"] == "unknown"
    assert service.execute(*agreement(tmp_path))["code"] == 5
    assert len(runner.merges) == 1


def test_concurrent_second_worker_cannot_submit_another_merge(store, tmp_path):
    """C-19.1, C-23.13: a second worker sees the executing operation and dispatches nothing."""
    runner = FakeGh()
    service = MergeActions(store, runner=runner)
    state, round_state = agreement(tmp_path)
    nested = []
    runner.on_merge = lambda: nested.append(service.execute(state, round_state, holder="other"))
    assert service.execute(state, round_state, holder="first")["code"] == 0
    assert nested[0]["code"] == 5 and len(runner.merges) == 1
    assert len(store.query("SELECT * FROM actions")) == 1


def test_changed_holder_cannot_publish_a_remote_result(store, tmp_path):
    """C-23.13: stale worker output is discarded and recorded, preserving current holder authority."""
    runner = FakeGh()
    service = MergeActions(store, runner=runner)

    def steal_holder():
        row = store.one("SELECT * FROM actions")
        request = json.loads(row["request_json"])
        request["holder"] = "new-holder"
        store.update_action(row["action_id"], request_json=json.dumps(request))

    runner.on_merge = steal_holder
    result = service.execute(*agreement(tmp_path), holder="old-holder")
    assert result["code"] == 5
    action = store.get_action(result["action_id"])
    assert action["state"] == "executing" and action["result_json"] is None
    assert store.query("SELECT * FROM events WHERE kind='action.result-discarded'")


def test_changed_holder_during_preflight_cannot_send_merge(store, tmp_path):
    """C-23.13: lost holder authority during remote preflight prevents the external action."""
    runner = FakeGh()
    service = MergeActions(store, runner=runner)

    def steal_holder():
        row = store.one("SELECT * FROM actions")
        request = json.loads(row["request_json"])
        request["holder"] = "new-holder"
        store.update_action(row["action_id"], request_json=json.dumps(request))

    runner.on_view = steal_holder
    assert service.execute(*agreement(tmp_path), holder="old-holder")["code"] == 5
    assert runner.merges == []


@pytest.mark.parametrize("change_during_read", [False, True])
def test_reconciliation_is_fenced_by_original_holder(store, tmp_path, change_during_read):
    """C-23.13: only the original durable holder can publish a remote-read reconciliation."""
    runner = FakeGh()
    runner.timeout = True
    service = MergeActions(store, runner=runner)
    result = service.execute(*agreement(tmp_path), holder="original-holder")
    runner.landed()

    def steal_holder():
        row = store.get_action(result["action_id"])
        request = json.loads(row["request_json"])
        request["holder"] = "new-holder"
        store.update_action(row["action_id"], request_json=json.dumps(request))

    if change_during_read:
        runner.on_view = steal_holder
    settled = service.reconcile(result["action_id"], holder="original-holder" if change_during_read else "intruder")
    assert settled["code"] == 5 and settled["action"]["status"] == "unknown"
    assert store.query("SELECT * FROM events WHERE kind='action.result-discarded'")
    assert not store.query("SELECT * FROM events WHERE kind='action.reconciled'")


@pytest.mark.parametrize("invalid", ["method", "main", "fingerprint", "peer", "findings", "notes"])
def test_invalid_agreement_never_creates_action_or_calls_remote(store, tmp_path, invalid):
    """C-23.8–9, C-23.11–12: only exact main/peer agreement authorizes merge or squash."""
    runner = FakeGh()
    state, round_state = agreement(tmp_path)
    if invalid == "method":
        state["merge_method"] = "rebase"
    elif invalid == "main":
        round_state["main_approval"]["approved"] = False
    elif invalid == "fingerprint":
        round_state["main_approval"]["expected_revision"]["head_sha"] = OTHER
    elif invalid == "peer":
        round_state["verdict"]["verdict"] = "changes_requested"
    else:
        round_state["verdict"][invalid] = ["must fix"]
    with pytest.raises(GateError):
        MergeActions(store, runner=runner).execute(state, round_state)
    assert not store.query("SELECT * FROM actions") and runner.commands == []


def test_dry_run_does_not_create_or_advance_action(store, tmp_path):
    """C-19.1: dry-run reports the operation fingerprint without commands or database mutation."""
    runner = FakeGh()
    service = MergeActions(store, runner=runner)
    before = store.list_events()
    result = service.execute(*agreement(tmp_path), dry_run=True)
    assert result["code"] == 0 and result["revision"] == REVISION
    assert result["op_key"] == f"{REPO}:42:{HEAD}"
    assert store.list_events() == before
    assert not store.query("SELECT * FROM actions") and runner.commands == []


@pytest.mark.parametrize("original_state", ["pending", "executing"])
def test_startup_recovers_orphan_action_without_submitting_or_forging_result(store, tmp_path, original_state):
    """C-19.1, C-23.13: pending aborts; lost execution becomes unknown until immutable read settlement."""
    runner = FakeGh()
    state, _ = agreement(tmp_path)
    store.add_action(action_id="orphan", kind="merge", op_key=f"{REPO}:42:{HEAD}", subject=f"{REPO}#42",
                     state=original_state, request_json=json.dumps({"holder": "dead-worker", **state,
                                                                    "approved_revision": REVISION}))
    service = MergeActions(store, runner=runner)
    assert service.recover() == {"recovered": ["orphan"]}
    assert runner.commands == []
    action = store.get_action("orphan")
    if original_state == "pending":
        assert action["state"] == "failed"
        assert json.loads(action["result_json"])["status"] == "blocked"
    else:
        assert action["state"] == "unknown" and action["result_json"] is None
        assert json.loads(action["request_json"])["holder"] == "dead-worker"
        runner.landed()
        assert service.reconcile("orphan")["code"] == 0
        assert store.get_action("orphan") == action
    assert service.recover() == {"recovered": []}
    assert runner.merges == []


@pytest.mark.parametrize("override", [
    {"url": "https://example.org/not-github"}, {"number": 43}, {"number": True},
    {"headRefOid": "short"}, {"baseRefOid": "short"}, {"isDraft": None},
    {"mergeCommit": "bad"}, {"url": "https://github.com/wrong/repo/pull/42"},
])
def test_remote_metadata_identity_and_types_fail_closed(tmp_path, override):
    """C-23.8, C-23.11: malformed or redirected metadata is never an approved PR revision."""
    with pytest.raises(GateError):
        capture_pr("42", cwd=tmp_path, repository=REPO, runner=FakeGh(**override))


def test_completed_landing_check_reads_parents_and_refuses_rebase(tmp_path):
    """C-23.12: completed gates verify immutable parents and reject unverifiable rebase landings."""
    runner = FakeGh()
    runner.landed()
    subject = capture_pr("42", cwd=tmp_path, runner=runner)
    state, _ = agreement(tmp_path)
    assert verify_landing(state, subject, REVISION, runner=runner) is None
    state["merge_method"] = "rebase"
    assert "method" in verify_landing(state, subject, REVISION, runner=runner)
