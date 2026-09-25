"""C-23.8–13, C-23.43/53: recovery and continuation at daemon gate boundaries."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from subfleet import cli
from subfleet.contracts import Decision
from subfleet.gate.service import GateService, dispatch
from subfleet.policy import DEFAULT_POLICY_PATH
from tests.fake.test_gate_end_to_end import arguments, finish, wire
from tests.unit.test_gate_admission import core, lane
from tests.unit.test_gate_merge import BASE, HEAD, LANDING, OTHER, FakeGh


def continued(gate_id, plan=None, *flags):
    """C-23.8: every resumed round carries the main's newly reviewed fingerprint."""
    argv = ["gate", "continue", gate_id, "--main-approve"]
    if plan is not None:
        argv += ["--expect-sha256", hashlib.sha256(plan.read_bytes()).hexdigest()]
    return cli.build_parser().parse_args([*argv, *flags])


def pause_after_certificate(core, started, monkeypatch):
    """C-3.2, C-23.8: interrupt after durable issuance but before completing the gate."""
    original = core._gate_service._save

    def save(state, transition):
        original(state, transition)
        if transition == "certificate-issued":
            raise OSError("simulated daemon crash after certificate publication")

    with monkeypatch.context() as patch:
        patch.setattr(core._gate_service, "_save", save)
        with pytest.raises(OSError, match="simulated daemon crash"):
            dispatch(core, "gate.poll", {"gate_id": started["gate_id"]})
    return core._gate_service._load(started["gate_id"])


def test_recovered_agreement_reissues_certificate_for_newly_approved_plan(core, tmp_path, monkeypatch):
    """C-23.8: a resumed changed fingerprint receives its own main/peer certificate."""
    plan = tmp_path / "plan.md"
    plan.write_text("First approved revision\n")
    first = dispatch(core, "gate.start", wire(arguments(plan)))
    finish(core, first)
    interrupted = pause_after_certificate(core, first, monkeypatch)
    old_revision = interrupted["certificate_content"]["artifact_revision"]
    plan.write_text("Second approved revision\n")
    core._gate_service = GateService(core)
    second = dispatch(core, "gate.continue", wire(continued(first["gate_id"], plan)))
    assert second["code"] is None and second["job_id"] != first["job_id"]
    finish(core, second)
    result = dispatch(core, "gate.poll", {"gate_id": first["gate_id"]})
    assert result["code"] == 0
    state = core._gate_service._load(first["gate_id"])
    new_revision = state["rounds"][-1]["revision"]
    assert new_revision != old_revision
    assert state["certificate_content"]["artifact_revision"] == new_revision
    certificate = json.loads((core.root / "gates" / first["gate_id"] / "certificate.json").read_text())
    assert certificate["main_approval"] == state["rounds"][-1]["main_approval"]
    assert certificate["peer_verdict"] == state["rounds"][-1]["verdict"]


def test_recovered_agreement_checks_source_before_claiming_completion(core, tmp_path, monkeypatch):
    """C-23.8: recovery must not report completion after its approved artifact changes."""
    plan = tmp_path / "plan.md"
    plan.write_text("Approved before interruption\n")
    first = dispatch(core, "gate.start", wire(arguments(plan)))
    finish(core, first)
    pause_after_certificate(core, first, monkeypatch)
    plan.write_text("Changed while the daemon was down\n")
    core._gate_service = GateService(core)
    result = dispatch(core, "gate.poll", {"gate_id": first["gate_id"]})
    assert result["code"] == 4
    assert len(core.store.list_jobs()) == 1


@pytest.mark.parametrize("key,record", [("downgrade", {}), ("downgrade", ""),
                                      ("downgraded", {}), ("downgraded", "")])
def test_present_empty_downgrade_record_never_becomes_a_verdict(core, tmp_path, key, record):
    """C-23.43: record presence matters even when a downgrade payload is empty."""
    plan = tmp_path / "plan.md"
    plan.write_text("One attested revision\n")
    started = dispatch(core, "gate.start", wire(arguments(plan, "--max-rounds", "1")))
    finish(core, started)
    attempt = core.store.list_attempts(started["job_id"])[-1]
    core.store.update_attempt(attempt["attempt_id"], evidence_json=json.dumps({key: record}))
    result = dispatch(core, "gate.poll", {"gate_id": started["gate_id"]})
    assert result["code"] == 4
    state = core._gate_service._load(started["gate_id"])
    assert state["rounds"][-1]["verdict"] is None
    assert "downgrade" in state["blocker"]


def test_replaced_round_lease_is_preserved_and_old_output_is_discarded(core, tmp_path):
    """C-23.10: consuming a stale round cannot delete a replacement holder's lease."""
    plan = tmp_path / "plan.md"
    plan.write_text("Lease protected\n")
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    finish(core, started)
    state = core._gate_service._load(started["gate_id"])
    lease_key = state["rounds"][-1]["round_lease"]
    core.store.release_leases(f"gate-round:{started['job_id']}")
    core.store.acquire_lease(lease_key, "replacement-holder")
    result = dispatch(core, "gate.poll", {"gate_id": started["gate_id"]})
    assert result["code"] == 4
    assert core.store.one("SELECT holder FROM leases WHERE lease_key=?", (lease_key,))["holder"] == "replacement-holder"
    assert core._gate_service._load(started["gate_id"])["rounds"][-1]["verdict"] is None


def test_continue_cannot_consume_finished_peer_with_wrong_explicit_fingerprint(core, tmp_path):
    """C-23.8: an explicit wrong expectation is rejected before consuming finished output."""
    plan = tmp_path / "plan.md"
    plan.write_text("The actual reviewed fingerprint\n")
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    finish(core, started)
    args = continued(started["gate_id"], plan)
    args.expect_sha256 = "f" * 64
    result = dispatch(core, "gate.continue", wire(args))
    assert result["code"] == 4
    assert not (core.root / "gates" / started["gate_id"] / "certificate.json").exists()


@pytest.mark.parametrize("attestation", ["mismatch", "unattested"])
@pytest.mark.parametrize("peer", ["fable", "opus"])
def test_claude_peer_requires_positive_attestation_at_the_service_boundary(core, tmp_path, attestation, peer):
    """C-23.43: a pinned Claude (Fable or Opus) job's mismatch/unattested output never counts as a verdict."""
    core.store.put_lane(lane(core.root / "claude-home", "claude"))
    decision = Decision((peer,), (), "claude-1", peer, "test", "test")
    core._pick = lambda *args, **kwargs: decision
    plan = tmp_path / "plan.md"
    plan.write_text(f"A {peer} peer must actually be attested\n")
    args = arguments(plan, "--max-rounds", "1")
    args.peer = peer
    started = dispatch(core, "gate.start", wire(args))
    finish(core, started, attestation=attestation)
    result = dispatch(core, "gate.poll", {"gate_id": started["gate_id"]})
    assert result["code"] == 4 and attestation in result["message"]
    state = core._gate_service._load(started["gate_id"])
    assert state["rounds"][-1]["verdict"] is None
    assert state["rounds"][-1]["requested_model"] == core.policy["models"][peer]["id"]


@pytest.mark.parametrize("peer", ["fable", "opus"])
def test_claude_peer_routing_reaches_the_round_and_an_attested_round_agrees(core, tmp_path, peer):
    """C-23.10 C-23.43: `--peer-account` pins the round to that lane and `--exclude-account`
    reaches its exclusions, for either Claude peer; an attested round issues the certificate."""
    core.store.put_lane(lane(core.root / "claude-home", "claude"))
    core._pick = lambda *args, **kwargs: Decision((peer,), (), "claude-1", peer, "test", "test")
    plan = tmp_path / "plan.md"
    plan.write_text(f"Route the {peer} round\n")
    started = dispatch(core, "gate.start", wire(arguments(
        plan, "--peer-account", "claude-1", "--exclude-account", "other@example.org", peer=peer)))
    assert started["code"] is None
    job = core.store.get_job(started["job_id"])
    assert (job["pinned_model"], job["pinned_lane"]) == (peer, "claude-1")
    assert "other@example.org" in json.loads(job["exclusions"])
    finish(core, started)
    assert dispatch(core, "gate.poll", {"gate_id": started["gate_id"]})["code"] == 0
    assert (core.root / "gates" / started["gate_id"] / "certificate.json").is_file()
    assert core._gate_service._load(started["gate_id"])["rounds"][-1]["peer_attestation"] == "attested"


def test_acceptance_artifact_tampering_blocks_without_reusing_export(core, tmp_path):
    """C-8.2, C-23.9: only immutable accepted bytes count, never a stale output export."""
    plan = tmp_path / "plan.md"
    plan.write_text("Immutable evidence\n")
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    finish(core, started)
    job = core.store.get_job(started["job_id"])
    artifact = next(row for row in core.store.list_artifacts(job["accepted_attempt_id"])
                    if row["role"] == "deliverable")
    Path(artifact["path"]).write_text("changed accepted output")
    result = dispatch(core, "gate.poll", {"gate_id": started["gate_id"]})
    assert result["code"] == 4 and "changed after acceptance" in result["message"]
    assert core._gate_service._load(started["gate_id"])["rounds"][-1]["verdict"] is None


def test_policy_cap_cannot_be_removed_by_zero_or_large_continue_override(core, tmp_path):
    """C-23.53: after four findings rounds, neither 0 nor a larger CLI limit dispatches."""
    plan = tmp_path / "plan.md"
    plan.write_text("Revision 1\n")
    result = dispatch(core, "gate.start", wire(arguments(plan, "--max-rounds", "0")))
    gate_id = result["gate_id"]
    for number in range(1, 5):
        finish(core, result, verdict="changes_requested")
        result = dispatch(core, "gate.poll", {"gate_id": gate_id})
        assert result["code"] == (4 if number == 4 else 3)
        if number < 4:
            plan.write_text(f"Revision {number + 1}\n")
            result = dispatch(core, "gate.continue", wire(continued(gate_id, plan)))
    for override in ("0", "1000"):
        plan.write_text("A fifth proposed revision\n")
        result = dispatch(core, "gate.continue", wire(continued(gate_id, plan, "--max-rounds", override)))
        assert result["code"] == 4
        assert "changes_requested" in result["message"]
        assert len(core.store.list_jobs()) == 4


class ChangingHeadGh(FakeGh):
    """C-23.11: fake GitHub permits a new approved head after a failed preflight."""

    def __call__(self, command, **kwargs):
        """C-19.1: every invoked remote or git command remains a local fixture."""
        if command[:2] == ["git", "diff"]:
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 0, "diff --git a/file b/file\n+change\n", "")
        if command[:3] == ["gh", "pr", "merge"]:
            self.commands.append(command)
            assert command[command.index("--match-head-commit") + 1] == self.metadata["headRefOid"]
            self.metadata.update(state="MERGED", mergeCommit={"oid": LANDING}, baseRefOid=OTHER)
            return subprocess.CompletedProcess(command, 0, "", "")
        return super().__call__(command, **kwargs)


def test_changed_pr_after_blocked_preflight_can_receive_fresh_peer_approval(core, tmp_path):
    """C-23.8, C-19.1: a newly attested PR head gets a new review and operation key."""
    runner = ChangingHeadGh(mergeStateStatus="BLOCKED")
    core.gate_runner = runner
    workdir = tmp_path / "source"
    workdir.mkdir()
    args = cli.build_parser().parse_args([
        "gate", "pr", "42", "--peer", "astra", "--main-approve", "--expect-head", HEAD,
        "--expect-base", BASE, "--on-agreement", "merge", "--merge-method", "squash", "-C", str(workdir),
    ])
    first = dispatch(core, "gate.start", wire(args))
    finish(core, first)
    blocked = dispatch(core, "gate.poll", {"gate_id": first["gate_id"]})
    assert blocked["code"] == 4 and runner.merges == []
    runner.local_head = OTHER
    runner.metadata.update(headRefOid=OTHER, mergeStateStatus="CLEAN")
    next_args = continued(first["gate_id"], None, "--expect-head", OTHER, "--expect-base", BASE)
    second = dispatch(core, "gate.continue", wire(next_args))
    assert second["code"] is None and second["job_id"] != first["job_id"]
    finish(core, second)
    completed = dispatch(core, "gate.poll", {"gate_id": first["gate_id"]})
    assert completed["code"] == 0
    state = core._gate_service._load(first["gate_id"])
    assert state["certificate_content"]["artifact_revision"]["head_sha"] == OTHER
    actions = core.store.query("SELECT * FROM actions")
    assert len(actions) == 2 and len(runner.merges) == 1
    assert {action["op_key"] for action in actions} == {f"example/project:42:{head}" for head in (HEAD, OTHER)}


def test_gate_protocol_missing_fields_are_invalid_input(core):
    """C-16.2, C-17.1: malformed gate clients receive 2 before any state changes."""
    for op in ("gate.start", "gate.continue"):
        result = dispatch(core, op, {})
        assert result["code"] == 2
    assert core.store.list_jobs() == []


def test_gate_protocol_dry_run_does_not_admit_or_write(core, tmp_path):
    """C-19.1: direct socket dry-run requests cannot create gate jobs or actions."""
    plan = tmp_path / "plan.md"
    plan.write_text("Preview only\n")
    before = core.store.list_events()
    result = dispatch(core, "gate.start", wire(arguments(plan, "--dry-run")))
    assert result["code"] == 0 and result["dry_run"]
    assert core.store.list_events() == before
    assert core.store.list_jobs() == []
    assert not (core.root / "gates").exists()


@pytest.mark.parametrize("change,message", [
    ({"peer": "sonnet"}, "--peer must be"),
    ({"main_model": "gpt-9"}, "unknown --main-model"),
])
def test_dry_run_refuses_what_start_refuses(core, tmp_path, change, message):
    """C-19.1, C-17.1: a socket dry-run refuses a peer or main model that start refuses, and writes nothing."""
    plan = tmp_path / "plan.md"
    plan.write_text("Preview a bad request\n")
    result = dispatch(core, "gate.start", {**wire(arguments(plan, "--dry-run")), **change})
    assert result["code"] == 2 and message in result["message"]
    assert core.store.list_jobs() == [] and not (core.root / "gates").exists()


def test_dry_run_resolves_a_retired_main_model(core, tmp_path):
    """C-19.1, C-17.1: `--main-model sol` resolves as `-m sol` does, in a preview too."""
    plan = tmp_path / "plan.md"
    plan.write_text("Preview a retired main\n")
    result = dispatch(core, "gate.start", wire(arguments(plan, "--dry-run", "--main-model", "sol")))
    assert result["code"] == 0 and result["dry_run"]


def test_opus_peer_may_review_a_same_family_main(core, tmp_path):
    """C-23.2, C-23.10: independence is the isolated round, so Opus may review an Opus main."""
    plan = tmp_path / "plan.md"
    plan.write_text("Independent opinion\n")
    result = dispatch(core, "gate.start", wire(arguments(plan, "--main-model", "opus", peer="opus")))
    assert result["code"] is None and result["job_id"]
    job = core.store.get_job(result["job_id"])
    assert (job["kind"], job["pinned_model"], job["sandbox"], job["isolated_review"]) == (
        "gate-review", "opus", "read-only", 1)
    state = core._gate_service._load(result["gate_id"])
    assert (state["peer"], state["main_family"]) == ("opus", "claude")
    assert state["rounds"][-1]["requested_model"] == core.policy["models"]["opus"]["id"]


DEFAULT_POLICY = json.loads(Path(DEFAULT_POLICY_PATH).read_text())
POLICY_MODELS = tuple(DEFAULT_POLICY["models"])
#: What `-m` accepts besides a short name: a retired alias, or an exact model id.
MAIN_ALIASES = {**DEFAULT_POLICY["retired"],
                **{model["id"]: short for short, model in DEFAULT_POLICY["models"].items()}}


@pytest.mark.parametrize("main_model", [None, *POLICY_MODELS, *MAIN_ALIASES, "gpt-9"])
@pytest.mark.parametrize("peer", ["fable", "opus", "astra", "sol"])
def test_every_peer_and_main_pair_is_admitted_or_refused_by_policy_alone(core, tmp_path, peer, main_model):
    """C-17.1 C-23.2 C-23.10 Over every peer and every main the policy names (and one it
    does not): no pair is refused for sharing a family; the round is an isolated read-only
    review pinned to the peer's policy model (`sol` is Astra); the main's family is its
    policy provider when named and unknown otherwise; an unnamed model is refused unsubmitted."""
    plan = tmp_path / "plan.md"
    plan.write_text(f"Pair {peer} with {main_model}\n")
    flags = ("--main-model", main_model) if main_model else ()
    result = dispatch(core, "gate.start", wire(arguments(plan, *flags, peer=peer)))
    short = MAIN_ALIASES.get(main_model, main_model)
    named = core.policy["models"].get(short) if main_model else None
    if main_model and named is None:
        assert result["code"] == 2 and "unknown --main-model" in result["message"]
        assert core.store.list_jobs() == []
        return
    assert result["code"] is None and result["job_id"]
    expected_peer = "astra" if peer == "sol" else peer
    job = core.store.get_job(result["job_id"])
    assert (job["kind"], job["pinned_model"], job["sandbox"], job["isolated_review"]) == (
        "gate-review", expected_peer, "read-only", 1)
    state = core._gate_service._load(result["gate_id"])
    assert (state["peer"], state["main_family"]) == (expected_peer, named["provider"] if named else None)
    assert state["rounds"][-1]["requested_model"] == core.policy["models"][expected_peer]["id"]


def test_gate_does_not_infer_main_family_from_the_peer(core, tmp_path):
    """C-23.8: without --main-model the main's family is unknown, never the peer's complement."""
    plan = tmp_path / "plan.md"
    plan.write_text("Unnamed main\n")
    result = dispatch(core, "gate.start", wire(arguments(plan)))
    assert result["code"] is None
    assert core._gate_service._load(result["gate_id"])["main_family"] is None


def test_unknown_main_model_is_refused(core, tmp_path):
    """C-17.1: an explicit main model must be one the policy names."""
    plan = tmp_path / "plan.md"
    plan.write_text("Who is the main?\n")
    result = dispatch(core, "gate.start", wire(arguments(plan, "--main-model", "gpt-9")))
    assert result["code"] == 2 and "unknown --main-model" in result["message"]
    assert core.store.list_jobs() == []


@pytest.mark.parametrize("peer", ["fable", "opus"])
def test_claude_peers_accept_account_routing(peer):
    """C-23.10: account routing applies to any Claude peer and to no Codex peer."""
    from subfleet.gate.service import GateError, routing
    args = cli.build_parser().parse_args(
        ["gate", "plan", "plan.md", "--peer", peer, "--peer-account", "a@example.org",
         "--exclude-account", "b@example.org"])
    assert routing(args, peer) == ("a@example.org", ("b@example.org",))
    with pytest.raises(GateError, match="Claude peer"):
        routing(args, "astra")


def test_offline_reader_understands_gate_schema(core):
    """C-3.5, C-17.5: additive gate migration remains readable by this CLI offline."""
    from subfleet.offline import KNOWN_SCHEMA_VERSION, Offline
    from subfleet.store import SCHEMA_VERSION
    assert KNOWN_SCHEMA_VERSION == SCHEMA_VERSION
    reader = Offline(core.root)
    assert reader.list_jobs() == []


def test_gate_actions_wait_for_daemon_recovery_before_mutating(core):
    """C-19.1, C-23.13: startup reconciliation cannot race a new action holder."""
    import threading
    core._recovery_complete = threading.Event()
    before = core.store.list_events()
    result = dispatch(core, "gate.continue", {"gate_id": "existing-gate"})
    assert result["code"] == 1 and "recovery" in result["message"]
    assert core.store.list_events() == before


@pytest.mark.parametrize("operation", ["gate.poll", "gate.continue"])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_landed_merge_recovers_before_gate_result_publication(core, tmp_path, monkeypatch, operation, ambiguous):
    """C-19.1, C-23.12–13: recover a landed holder result without checking the moving base or resubmitting."""
    class PatchGh(FakeGh):
        def __call__(self, command, **kwargs):
            if command[:2] == ["git", "diff"]:
                self.commands.append(command)
                return subprocess.CompletedProcess(command, 0, "diff --git a/file b/file\n+change\n", "")
            return super().__call__(command, **kwargs)

    runner = PatchGh()
    runner.timeout = ambiguous
    runner.on_merge = runner.landed
    core.gate_runner = runner
    source = tmp_path / "source"
    source.mkdir()
    args = cli.build_parser().parse_args([
        "gate", "pr", "42", "--peer", "astra", "--main-approve", "--expect-head", HEAD,
        "--expect-base", BASE, "--on-agreement", "merge", "--merge-method", "squash", "-C", str(source),
    ])
    started = dispatch(core, "gate.start", wire(args))
    finish(core, started)

    def crash(state, result):
        raise OSError("crash before gate result publication")

    with monkeypatch.context() as patch:
        patch.setattr(core._gate_service, "_action_result", crash)
        with pytest.raises(OSError, match="crash before gate result"):
            dispatch(core, "gate.poll", {"gate_id": started["gate_id"]})
    assert core._gate_service._load(started["gate_id"])["status"] == "agreed"
    action = core.store.query("SELECT * FROM actions")[0]
    assert action["state"] == ("unknown" if ambiguous else "confirmed")
    assert runner.metadata["baseRefOid"] != BASE
    core._gate_service = GateService(core)
    request = ({"gate_id": started["gate_id"]} if operation == "gate.poll" else
               wire(continued(started["gate_id"], None, "--expect-head", HEAD, "--expect-base", BASE)))
    recovered = dispatch(core, operation, request)
    assert recovered["code"] == 0 and recovered["status"] == "completed"
    assert recovered["action"]["action_id"] == action["action_id"]
    assert len(runner.merges) == 1 and len(core.store.query("SELECT * FROM actions")) == 1
    assert core.store.get_action(action["action_id"])["state"] == action["state"]


def test_poll_observes_completion_of_another_gates_existing_merge_action(core, tmp_path):
    """C-19.1, C-23.13: concurrent gates share one merge and observe its holder's result."""
    import concurrent.futures
    import threading

    class PatchGh(FakeGh):
        def __call__(self, command, **kwargs):
            if command[:2] == ["git", "diff"]:
                self.commands.append(command)
                return subprocess.CompletedProcess(command, 0, "diff --git a/file b/file\n+change\n", "")
            return super().__call__(command, **kwargs)

    runner = PatchGh()
    merging, release = threading.Event(), threading.Event()

    def pause_merge():
        merging.set()
        assert release.wait(5)

    runner.on_merge = pause_merge
    core.gate_runner = runner
    source = tmp_path / "source"
    source.mkdir()
    args = cli.build_parser().parse_args([
        "gate", "pr", "42", "--peer", "astra", "--main-approve", "--expect-head", HEAD,
        "--expect-base", BASE, "--on-agreement", "merge", "--merge-method", "squash", "-C", str(source),
    ])
    first = dispatch(core, "gate.start", wire(args))
    finish(core, first)
    second = dispatch(core, "gate.start", wire(args))
    finish(core, second)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(dispatch, core, "gate.poll", {"gate_id": first["gate_id"]})
        try:
            assert merging.wait(5)
            waiting = dispatch(core, "gate.poll", {"gate_id": second["gate_id"]})
            assert waiting["code"] == 5 and waiting["status"] == "action_attempting"
        finally:
            release.set()
        assert owner.result(timeout=5)["code"] == 0
    observed = dispatch(core, "gate.poll", {"gate_id": second["gate_id"]})
    assert observed["code"] == 0 and observed["status"] == "completed"
    assert len(runner.merges) == 1 and len(core.store.query("SELECT * FROM actions")) == 1
