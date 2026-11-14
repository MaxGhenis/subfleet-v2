"""C-11.10: the sandbox hold's exact wait, event, refusal and status contracts."""

from __future__ import annotations

import dataclasses
import json
import shlex

import pytest

from subfleet import cli, daemon as daemon_module, protocol, refusals, render, task_holds
from subfleet.client import DaemonError
from subfleet.contracts import Exit
from subfleet.offline import Offline
from tests.disk_floor_model import stamp
from tests.task_hold_support import fresh_daemon, store_image, submit, turn


SANDBOX = "workspace-write"
KEY = "sandbox-hold:workspace-write"
UNTIL = "2099-06-01T00:00:00Z"
HOLD = {"sandbox": SANDBOX, "until": stamp(100), "note": "disk brake", "since": stamp(0)}


def hold_sandbox(service, **changes):
    return service.dispatch("tasks", {"action": "hold", "sandbox": SANDBOX,
                                      "until": stamp(100), "note": "disk brake", **changes})


def release_sandbox(service, **changes):
    return service.dispatch("tasks", {"action": "release", "sandbox": SANDBOX, **changes})


def test_sandbox_hold_wait_and_release_hint_have_exact_fields():
    wait = {"reason": "task-held", "sandbox": SANDBOX, "until": stamp(100), "note": "disk brake",
            "next_check_at": stamp(100)}
    assert task_holds.wait_hold(HOLD) == wait
    assert task_holds.release_command(sandbox=SANDBOX) == "subfleet tasks release --sandbox workspace-write"
    lines = render.why_queue({"job_id": "20261024-000000-review", "kind": "dispatch", "state": "waiting",
                              "wait_reason": "task-held", "next_check_at": stamp(100), "hold": wait})
    assert lines[1] == (
        f"Held: the operator holds sandbox workspace-write jobs at admission until {stamp(100)} "
        "(disk brake); it starts once the hold ends or `subfleet tasks release --sandbox workspace-write`, "
        "and holds no other job back (C-11.10)")
    command = lines[1].split("`")[1]
    assert shlex.split(command) == ["subfleet", "tasks", "release", "--sandbox", SANDBOX]
    parsed = cli.build_parser().parse_args(shlex.split(command)[1:])
    assert (parsed.tasks_command, parsed.task, parsed.sandbox) == ("release", None, SANDBOX)


def test_tasks_args_coercion_keeps_the_sandbox_discriminator():
    args = protocol.coerce_args(protocol.TasksArgs, {"action": "hold", "sandbox": SANDBOX,
                                                   "until": stamp(100), "note": "disk brake"})
    assert dataclasses.asdict(args) == {"action": "hold", "task": None, "until": stamp(100),
                                       "note": "disk brake", "if_note_prefix": None, "sandbox": SANDBOX}
    assert dataclasses.asdict(protocol.coerce_args(protocol.TasksArgs, {})) == {
        "action": "list", "task": None, "until": None, "note": None,
        "if_note_prefix": None, "sandbox": None}


@pytest.mark.parametrize("task,source,expected", [
    ("build", "read-only", "read-only"),
    ("review", "workspace-write", "workspace-write"),
    ("build", protocol.POLICY_SANDBOX, "workspace-write"),
    ("research", protocol.POLICY_SANDBOX, "read-only"),
], ids=("explicit-read-only-build", "explicit-writable-review", "default-build", "wildcard-research"))
def test_submission_resolves_the_row_once_and_launch_spec_keeps_it_after_policy_changes(task, source, expected):
    """Explicit -s overrides and policy defaults share the stored admission/launch value."""
    with fresh_daemon() as (service, harness, clock, signals), pytest.MonkeyPatch.context() as patch:
        # These cases exercise submission without running Git or preparing a
        # worktree. A policy-default review has a separate conditional copy rule;
        # the explicit writable review here must not enter that rule.
        head = "a" * 40
        patch.setattr(daemon_module, "git_head", lambda *args, **kwargs: head)
        patch.setattr(daemon_module, "git_toplevel", lambda workdir, *args, **kwargs: str(workdir))
        patch.setattr(daemon_module, "git_common_dir", lambda *args, **kwargs: None)
        patch.setattr(daemon_module, "_git_prefix", lambda *args, **kwargs: ".")

        def no_copy_plan(*args, **kwargs):
            raise AssertionError("these sandbox submission cases must not plan an uncommitted review copy")

        def no_guardian(*args, **kwargs):
            raise AssertionError("sandbox submission contract must never spawn a guardian")

        patch.setattr(daemon_module.uncommitted, "plan", no_copy_plan)
        patch.setattr(service, "_spawn_guardian", no_guardian)
        assert service.policy["permissions"]["build"] == "workspace-write"
        assert service.policy["permissions"]["*"] == "read-only"
        args = harness.submit_args(task=task, tier="standard", sandbox=source, in_place=False)
        result = service.dispatch("submit", args)
        ident = result["job_id"]
        row = service.store.get_job(ident)
        assert result["sandbox"] == row["sandbox"] == expected
        assert service._spec(row).sandbox.value == expected
        manifest = json.loads((service.root / "jobs" / ident / "manifest.json").read_text())
        assert manifest["job"]["sandbox"] == expected

        # A new submission would now resolve to the opposite policy sandbox.
        # Existing admission and every attempt's JobSpec retain the stored row.
        opposite = "read-only" if expected == "workspace-write" else "workspace-write"
        service.policy["permissions"] = {"*": opposite}
        policy_args = protocol.coerce_args(protocol.SubmitArgs, {**args, "sandbox": protocol.POLICY_SANDBOX})
        assert service._policy_sandbox(policy_args) == opposite
        current = service.store.get_job(ident)
        assert current["sandbox"] == expected and service._spec(current).sandbox.value == expected
        hold_sandbox(service)
        with service.store.transaction("test.resolved_sandbox_hold") as tx:
            current_hold = task_holds.job_held(tx, ident, stamp(0))
        assert (current_hold is not None) == (expected == "workspace-write")


def test_sandbox_hold_and_release_reuse_the_existing_event_names_and_fields():
    with fresh_daemon() as (service, harness, clock, signals):
        ident = submit(service, harness, "review", "sandbox-event-review")
        with service.store.transaction("test.writable_job", job_id=ident) as tx:
            tx.execute("UPDATE jobs SET sandbox=? WHERE job_id=?", (SANDBOX, ident))
        before_job = service.store.get_job(ident)
        assert hold_sandbox(service) == {"held": SANDBOX, "task_hold": HOLD, "replaced": None,
                                         "task_holds": [HOLD]}
        assert service.store.query("SELECT * FROM leases WHERE lease_key=?", (KEY,)) == [{
            "lease_key": KEY, "holder": "operator: disk brake", "acquired_at": stamp(0),
            "expires_at": stamp(100)}]
        service._admit()
        assert service._holds[ident] == {"reason": "task-held", "sandbox": SANDBOX, "until": stamp(100),
                                         "note": "disk brake", "next_check_at": stamp(100)}
        clock[0] = 10
        assert release_sandbox(service) == {"released": SANDBOX, "was_held": True,
                                            "woken": 1, "task_holds": []}
        found = [(row["kind"], json.loads(row["data_json"])) for row in service.store.query(
            "SELECT kind,data_json FROM events WHERE kind IN "
            "('task.held','job.task_held','task.released','job.task_hold_ended') ORDER BY event_id")]
        assert found == [
            ("job.task_held", {"sandbox": SANDBOX, "until": stamp(100), "since": stamp(0),
                               "taken": True, "kept": None}),
            ("task.held", {"sandbox": SANDBOX, "until": stamp(100), "note": "disk brake", "replaced": None}),
            ("job.task_hold_ended", {"by": "release", "sandbox": SANDBOX,
                                     "ended_at": stamp(10), "next_check_at": None}),
            ("task.released", {"sandbox": SANDBOX, "until": stamp(100), "note": "disk brake", "woken": 1}),
        ]
        mutable = {"state", "wait_reason", "next_check_at"}
        assert {key: value for key, value in service.store.get_job(ident).items() if key not in mutable} == {
            key: value for key, value in before_job.items() if key not in mutable}


def test_sandbox_condition_refusal_has_exact_checked_wire_details():
    error = protocol.ProtocolError("private diagnostic", Exit.REFUSED, reason="task-hold-condition",
                                   details={"task_hold": HOLD})
    response = protocol.decode_response(protocol.encode(protocol.refuse("sandbox-condition", error)))
    assert response.ok is False
    assert dataclasses.asdict(response.error) == {
        "code": 7,
        "message": (f"tasks: hold note does not match --if-note-prefix: sandbox workspace-write, "
                    f"until {stamp(100)}, note 'disk brake', since {stamp(0)}"),
        "fix": None, "reason": "task-hold-condition", "details": {"task_hold": HOLD},
    }


@pytest.mark.parametrize("invalid", [
    {**HOLD, "sandbox": "read-only"},
    {**HOLD, "sandbox": ""},
    {**HOLD, "sandbox": None},
    {**HOLD, "sandbox": 1},
    {**HOLD, "task": "review"},
    {**HOLD, "extra": "value"},
    {key: value for key, value in HOLD.items() if key != "sandbox"},
])
def test_sandbox_condition_wire_rejects_invalid_or_mixed_discriminators(invalid):
    details = {"task_hold": invalid}
    assert refusals.task_hold_condition_details(details) is None
    error = protocol.ProtocolError("private diagnostic", Exit.REFUSED, reason="task-hold-condition",
                                   details=details)
    response = protocol.decode_response(protocol.encode(protocol.refuse("sandbox-condition", error)))
    assert response.error.code == 7
    assert response.error.reason is None and response.error.details is None
    assert response.error.message == "tasks: hold note does not match --if-note-prefix"


def test_cli_json_condition_refusal_preserves_sandbox_details(monkeypatch, capsys):
    class Client:
        def call(self, op, args):
            if op == "capabilities":
                return {"capabilities": [protocol.TASKS_IF_NOTE_PREFIX_CAPABILITY]}
            assert op == "tasks" and args["sandbox"] == SANDBOX
            response = protocol.refuse("sandbox-condition", protocol.ProtocolError(
                "private diagnostic", Exit.REFUSED, reason="task-hold-condition", details={"task_hold": HOLD}))
            error = response.error
            raise DaemonError(error.code, error.message, error.fix, reason=error.reason, details=error.details)

    monkeypatch.setattr(cli, "_client", lambda args: Client())
    assert cli.main(["tasks", "release", "--sandbox", SANDBOX,
                     "--if-note-prefix", "other:", "--json"]) == 7
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"refused": "task-hold-condition", "task_hold": HOLD}
    assert "sandbox workspace-write" in captured.err and "task-hold-condition" in captured.err


@pytest.mark.parametrize("action", ("hold", "release"))
@pytest.mark.parametrize("target", [
    {"task": "review", "sandbox": SANDBOX},
    {"task": "", "sandbox": SANDBOX},
    {"sandbox": "read-only"},
    {"sandbox": ""},
    {"sandbox": 1},
])
def test_daemon_refuses_mixed_or_unsupported_targets_without_mutation(action, target):
    with fresh_daemon() as (service, harness, clock, signals):
        hold_sandbox(service)
        before = store_image(service)
        with pytest.raises(protocol.ProtocolError) as error:
            service.dispatch("tasks", {"action": action, "until": stamp(200), "note": "second brake", **target})
        assert (error.value.reason, error.value.code) == ("tasks-target", 2)
        assert protocol.refusal_text(error.value) == (
            "tasks: choose a task or --sandbox workspace-write, never both", None)
        assert store_image(service) == before


@pytest.mark.parametrize("reader", ("daemon", "offline"))
def test_status_counts_all_sandbox_held_jobs_and_excludes_exempt_rows(reader):
    """Count the complete store even when offline status returns only 50 jobs."""
    with fresh_daemon() as (service, harness, clock, signals):
        for index in range(51):
            ident = submit(service, harness, "review", f"held-review-{index}")
            with service.store.transaction("test.writable_job", job_id=ident) as tx:
                tx.execute("UPDATE jobs SET sandbox=? WHERE job_id=?", (SANDBOX, ident))
        submit(service, harness, "review", "read-only-review")
        taskless = submit(service, harness, None, "taskless-writer")
        conversation = turn(service, harness, 1, task="review")
        hold_sandbox(service, until=UNTIL)
        # Reader protection against stale metadata: even if old waiting rows
        # name task-held, the turn and taskless exclusions still apply.
        with service.store.transaction("test.stale_exempt_waits") as tx:
            tx.execute("UPDATE jobs SET sandbox=?,state='waiting',wait_reason='task-held',next_check_at=? "
                       "WHERE job_id IN (?,?)", (SANDBOX, UNTIL, taskless, conversation))
        status = service.dispatch("daemon.status", {}) if reader == "daemon" else Offline(service.root).status()
        assert status["task_holds"] == [{"sandbox": SANDBOX, "until": UNTIL, "note": "disk brake",
                                         "since": stamp(0), "waiting": 51}]
        if reader == "offline":
            assert len(status["running"]) == 50
        assert "workspace-write until 2099-06-01T00:00:00Z (51 waiting)" in cli.format_status(status)
        counts = task_holds.waiting_counts(service.store.query(task_holds.WAITING_SQL))
        assert counts[task_holds.index_key(HOLD)] == 51
