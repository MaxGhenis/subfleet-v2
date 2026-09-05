"""C-17.1, C-23.8–10, C-23.43/53: gate clients against a deterministic daemon."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from subfleet import cli, protocol
from subfleet.gate import cli as gate_cli
from subfleet.gate.certificate import write_bytes
from subfleet.gate.service import dispatch, GateService
from subfleet.store import utc_now
from tests.unit.test_gate_admission import core  # Real submit/admit; fake lane/process boundary.

FIXTURE = Path(__file__).parents[1] / "fixtures/gates/peer-verdict.txt"


def arguments(plan, *flags):
    return cli.build_parser().parse_args(["gate", "plan", str(plan), "--peer", "astra",
        "--main-approve", "--expect-sha256", hashlib.sha256(plan.read_bytes()).hexdigest(), *flags])


def wire(args):
    return {k: v for k, v in vars(args).items() if k not in {"handler", "command", "version"}}


def finish(core, result, *, attestation="attested", verdict="approve", transform=None):
    """C-4.3: the fake peer offers an immutable accepted artifact from its own job."""
    core._admit()
    job_id = result["job_id"]
    job = core.store.get_job(job_id)
    attempt = core.store.list_attempts(job_id)[-1]
    directory = core.root / "jobs" / job_id / "a1"
    rev = json.loads((Path(job["workdir"]) / "artifact.json").read_text())
    text = FIXTURE.read_text().replace("REVISION", json.dumps(rev)).replace('"approve"', json.dumps(verdict))
    if verdict == "changes_requested":
        text = text.replace('"findings":[]', '"findings":[{"severity":"high","location":"plan","description":"Add rollback"}]')
    if transform:
        text = transform(text)
    path = directory / "deliverable.md"
    write_bytes(path, text.encode())
    with core.store.transaction("fake-peer.accepted", job_id=job_id):
        core.store.add_artifact(attempt["attempt_id"], "deliverable", str(path), hashlib.sha256(text.encode()).hexdigest(), len(text.encode()))
        core.store.update_attempt(attempt["attempt_id"], state="succeeded", rc=0,
            attestation=attestation, model_served=attempt["model_requested"], finished_at=utc_now())
        core.store.update_job(job_id, state="succeeded", accepted_attempt_id=attempt["attempt_id"], rc=0, finished_at=utc_now())
    core._export(job_id)


class FakeClient:
    """C-16.1: real daemon dispatcher, with a fixture-driven provider boundary."""
    def __init__(self, core, *, on_round=None, attestation="attested", verdict="approve"):
        self.core, self.calls, self.jobs = core, [], []
        self.on_round, self.attestation, self.verdict = on_round, attestation, verdict

    def call(self, op, args, **kwargs):
        self.calls.append(op)
        result = dispatch(self.core, op, args)
        if result.get("code") is None and result.get("job_id") not in self.jobs:
            self.jobs.append(result["job_id"])
            if self.on_round:
                self.on_round(result)
            finish(self.core, result, attestation=self.attestation, verdict=self.verdict)
        return result


def test_plan_cli_round_replays_fixture_through_job_store(core, tmp_path):
    """C-17.1, C-23.8–10: one job owns copied inputs and produces the v1 certificate."""
    plan = tmp_path / "plan.md"
    plan.write_text("A concrete implementation plan.\n")
    client = FakeClient(core)
    assert gate_cli.run(arguments(plan), root=core.root, client=client, poll_interval=0) == 0
    jobs = core.store.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert (job["kind"], job["pinned_model"], job["sandbox"], job["isolated_review"]) == ("gate-review", "astra", "read-only", 1)
    assert Path(job["workdir"]).is_relative_to(core.root / "reviews")
    assert (Path(job["workdir"]) / "artifact.snapshot").read_bytes() == plan.read_bytes()
    gate_dir = next((core.root / "gates").iterdir())
    state = json.loads((gate_dir / "gate.json").read_text())
    certificate = json.loads((gate_dir / "certificate.json").read_text())
    assert certificate["artifact_revision"] == certificate["main_approval"]["expected_revision"]
    assert certificate["peer_verdict"]["artifact_revision"] == certificate["artifact_revision"]
    assert state["rounds"][0]["peer_run_id"] == job["job_id"]
    assert not core.store.query("SELECT * FROM leases WHERE lease_key LIKE 'gate:%'")
    transitions = [json.loads(r["data_json"]).get("transition") for r in core.store.query("SELECT data_json FROM events WHERE kind='gate.state'")]
    assert {"created", "round-prepared", "round-submitted", "round-finished", "certificate-issued", "completed"} <= set(transitions)
    assert core.store.query("SELECT * FROM actions") == []


def test_round_lease_refuses_concurrent_continue(core, tmp_path):
    """C-23.10: a concurrent caller cannot launch or replace a live reserved round."""
    plan = tmp_path / "plan.md"
    plan.write_text("One revision\n")
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    core._admit()
    gate_id = started["gate_id"]
    argv = cli.build_parser().parse_args(["gate", "continue", gate_id, "--main-approve", "--expect-sha256", hashlib.sha256(plan.read_bytes()).hexdigest()])
    result = dispatch(core, "gate.continue", wire(argv))
    assert result["code"] == 4 and "already running" in result["message"]
    assert len(core.store.list_jobs()) == 1
    assert len(core._gate_service._load(gate_id)["rounds"]) == 1


def test_plan_dry_run_submits_nothing_and_creates_no_state(core, tmp_path, capsys):
    """C-19.1, C-23.8: dry-run prints a fingerprint without approving or mutating."""
    plan = tmp_path / "plan.md"
    plan.write_text("Preview\n")
    args = arguments(plan, "--dry-run", "--json")
    args.main_approve = False
    client = FakeClient(core)
    before = core.store.list_events()
    assert gate_cli.run(args, root=core.root, client=client) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["revision"]["sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    assert len(payload["fingerprint"]) == 64
    assert client.calls == [] and core.store.list_jobs() == []
    assert core.store.list_events() == before and not (core.root / "gates").exists()


def test_changed_plan_during_peer_blocks_and_discards_approval(core, tmp_path):
    """C-23.8: re-capture after peer completion blocks an approval of changed bytes."""
    plan = tmp_path / "plan.md"
    plan.write_text("Original\n")
    client = FakeClient(core, on_round=lambda _: plan.write_text("Changed\n"))
    assert gate_cli.run(arguments(plan), root=core.root, client=client, poll_interval=0) == 4
    assert not list((core.root / "gates").glob("*/certificate.json"))


@pytest.mark.parametrize("attestation", ["mismatch", "unattested"])
def test_unattested_peer_retries_then_stops_at_policy_cap(core, tmp_path, attestation, capsys):
    """C-23.43, C-23.53: discarded output never becomes a verdict; four jobs stop."""
    plan = tmp_path / "plan.md"
    plan.write_text("Review me\n")
    client = FakeClient(core, attestation=attestation)
    assert gate_cli.run(arguments(plan), root=core.root, client=client, poll_interval=0) == 4
    assert len(client.jobs) == 4
    state = core._gate_service._load(next((core.root / "gates").iterdir()).name)
    assert all(r["verdict"] is None for r in state["rounds"])
    assert "not a verdict" in state["blocker"]
    assert attestation in capsys.readouterr().out


def test_lost_round_lease_discards_previously_successful_output(core, tmp_path):
    """C-23.10: even valid attested output has no authority after lease loss."""
    plan = tmp_path / "plan.md"
    plan.write_text("Exact bytes\n")
    result = dispatch(core, "gate.start", wire(arguments(plan)))
    finish(core, result)
    core.store.release_leases(f"gate-round:{result['job_id']}")
    result = dispatch(core, "gate.poll", {"gate_id": result["gate_id"]})
    assert result["code"] == 4 and "lease" in result["message"]
    assert not list((core.root / "gates").glob("*/certificate.json"))


def test_changes_requested_continue_requires_new_approval_or_response(core, tmp_path):
    """C-23.8/9: findings return 3; continuation rebinds main approval to new bytes."""
    plan = tmp_path / "plan.md"
    plan.write_text("No rollback\n")
    first = FakeClient(core, verdict="changes_requested")
    assert gate_cli.run(arguments(plan), root=core.root, client=first, poll_interval=0) == 3
    gate_id = next((core.root / "gates").iterdir()).name
    args = cli.build_parser().parse_args(["gate", "continue", gate_id, "--main-approve", "--expect-sha256", hashlib.sha256(plan.read_bytes()).hexdigest()])
    assert gate_cli.run(args, root=core.root, client=FakeClient(core), poll_interval=0) == 3
    plan.write_text("Rollback included\n")
    args.expect_sha256 = hashlib.sha256(plan.read_bytes()).hexdigest()
    assert gate_cli.run(args, root=core.root, client=FakeClient(core), poll_interval=0) == 0
    assert len(core.store.list_jobs()) == 2


def test_daemon_restart_reads_journal_and_does_not_redispatch(core, tmp_path):
    """C-3.2, C-23.10: daemon restart retains peer job ownership and journal evidence."""
    plan = tmp_path / "plan.md"
    plan.write_text("Restart safe\n")
    first = dispatch(core, "gate.start", wire(arguments(plan)))
    finish(core, first)
    directory = core.root / "gates" / first["gate_id"]
    (directory / "gate.json").write_text("partially published")
    core._gate_service = GateService(core)
    result = dispatch(core, "gate.poll", {"gate_id": first["gate_id"]})
    assert result["code"] == 0 and len(core.store.list_jobs()) == 1
    assert json.loads((directory / "gate.json").read_text())["status"] == "completed"


def test_plan_peer_process_finalizes_through_real_daemon(daemon):
    """C-5.2, C-17.1, C-23.8–10/43: a real guardian accepts a fixture peer via socket CLI."""
    import os
    import subprocess
    import sys

    daemon.start("--gate-peer")
    plan = daemon.workdir / "plan.md"
    plan.write_text("A process-backed agreement gate with exact revision ownership.\n")
    repository = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-m", "subfleet", "gate", "plan", str(plan), "--peer", "astra",
         "--main-model", "fable", "--main-approve", "--expect-sha256",
         hashlib.sha256(plan.read_bytes()).hexdigest(), "--json"],
        cwd=repository, env={**os.environ, "SUBFLEET_HOME": str(daemon.root),
                             "PYTHONPATH": str(repository)},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr, daemon.log_text())
    result = json.loads(completed.stdout)
    assert result["code"] == 0
    jobs = daemon.rows("SELECT * FROM jobs WHERE kind='gate-review'")
    assert len(jobs) == 1 and jobs[0]["state"] == "succeeded"
    job = jobs[0]
    attempt, = daemon.attempts(job["job_id"])
    assert attempt["attestation"] == "attested"
    assert attempt["model_requested"] == attempt["model_served"] == "gpt-6-astra"
    assert attempt["guardian_pid"] > 0 and attempt["child_pid"] > 0
    assert attempt["guardian_pid"] != attempt["child_pid"]
    directory = daemon.root / "jobs" / job["job_id"] / "a1"
    assert (directory / "start.json").is_file() and (directory / "exit.json").is_file()
    finalization = json.loads((directory / "finalization.json").read_text())
    assert finalization["attestation"]["status"] == "attested"
    assert "synthetic gate peer fixture evidence" in finalization["attestation"]["evidence"]
    certificate = json.loads((daemon.root / "gates" / result["gate_id"] / "certificate.json").read_text())
    assert certificate["artifact_revision"] == certificate["peer_verdict"]["artifact_revision"]
    assert certificate["artifact_revision"]["sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    deliverable, = daemon.rows("SELECT * FROM artifacts WHERE attempt_id=? AND role='deliverable'",
                               (attempt["attempt_id"],))
    assert Path(deliverable["path"]).read_text().startswith("---SUBFLEET-VERDICT-BEGIN---")
    assert daemon.rows("SELECT * FROM leases WHERE lease_key LIKE 'gate:%'") == []
    assert daemon.rows("SELECT * FROM actions") == []


def test_gate_fixture_child_publishes_only_its_explicit_synthetic_attestation(core, tmp_path):
    """C-12.8, C-23.9/43: the fake child replays the real bundle without manufacturing store state."""
    import os
    import subprocess

    from subfleet.contracts import Attestation, ExitInfo
    from subfleet.gate.verdict import parse_verdict
    from tests.fake.gate_peer import FakeGateAdapter

    plan = tmp_path / "plan.md"
    plan.write_text("A deterministic fixture peer.\n")
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    core._admit()
    job = core.store.get_job(started["job_id"])
    attempt, = core.store.list_attempts(job["job_id"])
    directory = core.root / "jobs" / job["job_id"] / "a1"
    directory.mkdir()
    adapter = FakeGateAdapter()
    launch = adapter.build_launch(core._spec(job), attempt["attempt_id"], directory,
                                  core.store.get_lane(attempt["lane_id"]), {},
                                  attempt["model_requested"], None, Path(job["prompt_path"]), None)
    repository = Path(__file__).resolve().parents[2]
    with open(launch.stdin_path, "rb") as stdin:
        completed = subprocess.run(
            launch.argv, cwd=launch.cwd, stdin=stdin, capture_output=True, timeout=3,
            env={**os.environ, **launch.env_add, "PYTHONPATH": str(repository),
                 "SUBFLEET_ATTEMPT": attempt["attempt_id"]},
        )
    assert completed.returncode == 0, completed.stderr
    Path(launch.stdout_path).write_bytes(completed.stdout)
    Path(launch.stderr_path).write_bytes(completed.stderr)
    outcome = adapter.classify(directory, launch, ExitInfo(0, None, 0, None))
    attestation = adapter.attest(directory, launch, outcome, attempt["model_requested"])
    assert attestation.status == Attestation.ATTESTED
    assert attestation.served_model == attempt["model_requested"]
    revision = json.loads((Path(job["workdir"]) / "artifact.json").read_text())
    assert parse_verdict(completed.stdout.decode(), revision)["verdict"] == "approve"
    assert core.store.get_job(job["job_id"])["accepted_attempt_id"] is None
