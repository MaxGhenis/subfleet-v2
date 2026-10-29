"""#277 review r2: status defaults and enrolment after an honoured launch."""

from pathlib import Path

from tests.fake.test_daemon_launch import launch_state  # noqa: F401
from tests.fake.test_launch_provider_qos import (CHOSEN, asks_inherit, guardian_commands,
                                               guardian_flags, launch_first, submit)


def test_status_capacity_rows_normalize_job_requests_only(launch_state):
    daemon, harness, _ = launch_state
    asked = submit(daemon, harness, provider_qos="default")
    plain = submit(daemon, harness)
    daemon._admit()
    view = daemon._capacity_rows()["view"]
    assert {row["job_id"]: row["provider_qos"] for row in view["jobs"]} == {
        asked: "default", plain: "utility"}
    assert all("provider_qos" not in row for row in view["attempts"])
    assert daemon.store.one("SELECT provider_qos FROM jobs WHERE job_id=?", (plain,))["provider_qos"] is None


def test_reenrollment_after_honoured_launch_keeps_guardian_default(launch_state, monkeypatch):
    daemon, harness, calls = launch_state
    daemon.policy["admission"]["priority_callers"] = [CHOSEN]
    job_id = submit(daemon, harness, caller_session=CHOSEN, provider_qos="default")
    _, command = launch_first(daemon, calls, job_id)
    assert asks_inherit(command)

    def awaited(record, child):
        for name in ("stdout.txt", "stderr.txt"):
            (Path(record["directory"]) / name).write_text("")
        return True, {"rc": 0, "signal": None}

    monkeypatch.setattr(daemon, "_await_probe", awaited)
    calls.clear()
    result = daemon._enrollment_turn("codex-1", "enroll:review-r2", ["codex", "login"],
                                     cwd=str(harness.workdir), env={}, timeout=5)
    assert result.returncode == 0
    [command] = guardian_commands(calls)
    assert "--provider-qos" not in guardian_flags(command)
