"""C-5.1, C-6.16, D-QOS-BOOKED1: the daemon asks for an unclamped provider only for an
honoured request, decided afresh at each launch.

A job requests `run --qos default` (`provider_qos`). At each launch of its provider the
daemon passes the guardian `--provider-qos inherit` only when the job requested `default`
and is `priority` at that launch (its caller, or an ancestor's, is in
`admission.priority_callers`). Every other launch asks nothing, so the guardian's own
default, the `utility` clamp, applies: other jobs, probes and re-enrolments alike. An
unhonoured request is no error: the job runs clamped and `why` says why.

Popen is recorded and no provider runs (tests/fake/test_daemon_launch.py's fixture); the
priorities a real launch gets are read in tests/fake/test_provider_qos_booked.py.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module
from subfleet.contracts import OutcomeClass, attempt_dir
from subfleet.guardian import atomic_publish
from tests.fake.test_daemon_launch import launch_state  # noqa: F401 (fixture)
from tests.fake.test_probe_recovery import reserved_probe
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401 (fixture)
from tests.fake_adapter import FakeAdapter

CHOSEN = "0a1b2c3d-0000-4000-8000-00000000c0de"
OTHER = "0a1b2c3d-0000-4000-8000-0000000000ff"


def guardian_commands(calls) -> list[list[str]]:
    return [list(command) for command, _ in calls if list(command)[1:3] == ["-m", "subfleet.guardian"]]


def guardian_flags(command: list[str]) -> list[str]:
    """The guardian's own options: everything before the provider argv."""
    return command[:command.index("--")]


def asks_inherit(command: list[str]) -> bool:
    flags = guardian_flags(command)
    if "--provider-qos" not in flags:
        return False
    return flags[flags.index("--provider-qos") + 1] == "inherit"


def submit(daemon, harness, **overrides) -> str:
    return daemon.dispatch("submit", harness.submit_args(**overrides))["job_id"]


def launch_first(daemon, calls, job_id) -> tuple[dict, list[str]]:
    daemon._admit()
    attempt = daemon.store.list_attempts(job_id)[-1]
    calls.clear()
    daemon._launch(attempt)
    [command] = guardian_commands(calls)
    return attempt, command


def launch_again(daemon, calls, attempt) -> tuple[dict, list[str]]:
    """The job's next attempt, reserved as admission reserves one, then launched."""
    row = {key: value for key, value in daemon.store.get_attempt(attempt["attempt_id"]).items()
           if key in ("job_id", "lane_id", "model_requested", "evidence_json")}
    seq = attempt["seq"] + 1
    daemon.store.update_attempt(attempt["attempt_id"], state="failed")
    daemon.store.add_attempt(row, attempt_id=f"{attempt['job_id']}/a{seq}", seq=seq)
    nxt = daemon.store.get_attempt(f"{attempt['job_id']}/a{seq}")
    calls.clear()
    daemon._launch(nxt)
    [command] = guardian_commands(calls)
    return nxt, command


@pytest.fixture
def priority(launch_state):  # noqa: F811
    daemon, harness, calls = launch_state
    daemon.policy["admission"]["priority_callers"] = [CHOSEN.upper()]
    return daemon, harness, calls


def test_d_qos_booked1_an_honoured_request_starts_its_provider_unclamped(priority):
    daemon, harness, calls = priority
    job_id = submit(daemon, harness, caller_session=CHOSEN, provider_qos="default")
    attempt, command = launch_first(daemon, calls, job_id)
    assert asks_inherit(command), command
    flags = guardian_flags(command)
    assert flags.count("--provider-qos") == 1
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "starting"


def test_d_qos_booked1_a_priority_ancestors_caller_is_honoured(priority):
    daemon, harness, calls = priority
    root = submit(daemon, harness, caller_session=CHOSEN)
    daemon.store.update_job(root, state="succeeded")       # finished ancestors count (C-6.16)
    child = submit(daemon, harness, caller_session=OTHER, parent_job_id=root, provider_qos="default")
    _, command = launch_first(daemon, calls, child)
    assert asks_inherit(command), command


@pytest.mark.parametrize("caller, requested, callers", [
    (OTHER, "default", [CHOSEN]),            # requested, but not a priority caller
    (CHOSEN, "default", None),               # no priority callers at all
    (CHOSEN, "default", []),
    (CHOSEN, "utility", [CHOSEN]),           # a priority caller that did not ask
    (CHOSEN, None, [CHOSEN]),
], ids=["not-listed", "no-list", "empty-list", "asked-utility", "asked-nothing"])
def test_d_qos_booked1_every_other_launch_asks_nothing(launch_state, caller, requested, callers):  # noqa: F811
    daemon, harness, calls = launch_state
    daemon.policy["admission"]["priority_callers"] = callers
    job_id = submit(daemon, harness, caller_session=caller, provider_qos=requested)
    attempt, command = launch_first(daemon, calls, job_id)
    assert "--provider-qos" not in guardian_flags(command), command
    # Not an error: the job launched, clamped.
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "starting"


def test_d_qos_booked1_each_attempt_decides_afresh(priority):
    daemon, harness, calls = priority
    job_id = submit(daemon, harness, caller_session=CHOSEN, provider_qos="default")
    first, command = launch_first(daemon, calls, job_id)
    assert asks_inherit(command)
    daemon.policy["admission"]["priority_callers"] = [OTHER]   # the caller leaves the list
    second, command = launch_again(daemon, calls, first)
    assert second["seq"] == 2 and "--provider-qos" not in guardian_flags(command), command
    daemon.policy["admission"]["priority_callers"] = [CHOSEN]  # and comes back
    third, command = launch_again(daemon, calls, second)
    assert third["seq"] == 3 and asks_inherit(command), command


def test_d_qos_booked1_a_failed_ancestry_read_opens_no_launch_gate(priority, monkeypatch):
    """The decision is read before the launch gate is made: a store that cannot answer
    leaves no descriptor open and starts no guardian (C-4.2, C-5.1)."""
    daemon, harness, calls = priority
    job_id = submit(daemon, harness, caller_session=CHOSEN, provider_qos="default")
    daemon._admit()
    attempt = daemon.store.list_attempts(job_id)[-1]
    gates = []
    real = daemon_module.procs.pipe_above_stdio
    monkeypatch.setattr(daemon_module.procs, "pipe_above_stdio", lambda: gates.append(real()) or gates[-1])

    def unreadable(jobs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(daemon, "_priority_jobs", unreadable)
    calls.clear()
    with pytest.raises(sqlite3.OperationalError):
        daemon._launch(attempt)
    assert gates == [] and guardian_commands(calls) == []

def test_d_qos_booked1_a_probe_for_an_honoured_job_stays_clamped(routing_state, monkeypatch):  # noqa: F811
    """C-11.4: a probe is the daemon's own read of a lane, never the job's work."""
    service, harness = routing_state
    service.policy["admission"]["priority_callers"] = [CHOSEN]
    job_id = submit(service, harness, tier="hard", caller_session=CHOSEN, provider_qos="default")
    record = reserved_probe(service, job_id, state="reserved")
    commands = []
    original_close = daemon_module.os.close
    monkeypatch.setattr(daemon_module.procs, "pipe_above_stdio", lambda: (800, 801))
    monkeypatch.setattr(daemon_module.os, "close", lambda fd: None if fd in (800, 801) else original_close(fd))
    original_write = daemon_module.os.write
    monkeypatch.setattr(daemon_module.os, "write", lambda fd, value: None if fd == 801 else original_write(fd, value))

    def spawn(command, **kwargs):
        commands.append(list(command))
        return SimpleNamespace(pid=900001)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", spawn)

    def awaited(value, child):
        value["state"] = "contained"
        service._save_probe(value)
        return True, {"rc": 0, "signal": None, "wall_s": .1, "child_pid": 900002}
    monkeypatch.setattr(service, "_await_probe", awaited)
    outcome = service._execute_probe(service.store.get_job(job_id), service.store.get_lane("codex-1"),
                                     service.policy["models"]["astra"], record["holder"])
    assert outcome.cls == OutcomeClass.OK
    [command] = [c for c in commands if c[1:3] == ["-m", "subfleet.guardian"]]
    assert "--provider-qos" not in guardian_flags(command), command


def test_d_qos_booked1_a_re_enrolment_stays_clamped(launch_state, monkeypatch):  # noqa: F811
    daemon, harness, calls = launch_state
    daemon.policy["admission"]["priority_callers"] = [CHOSEN]

    def awaited(record, child):
        # What a guardian that ran would have left: its provider's two streams.
        for name in ("stdout.txt", "stderr.txt"):
            (Path(record["directory"]) / name).write_text("")
        return True, {"rc": 0, "signal": None}
    monkeypatch.setattr(daemon, "_await_probe", awaited)
    result = daemon._enrollment_turn("codex-1", "enroll:fixture", ["codex", "login"], cwd=str(harness.workdir),
                                     env={}, timeout=5)
    assert result.returncode == 0
    [command] = guardian_commands(calls)
    assert "--provider-qos" not in guardian_flags(command), command


@pytest.mark.parametrize("value", ["inherit", "background", "DEFAULT", "", 1, True])
def test_d_qos_booked1_submit_refuses_any_other_request(launch_state, value):  # noqa: F811
    daemon, harness, _ = launch_state
    with pytest.raises(daemon_module.protocol.ProtocolError) as exc:
        submit(daemon, harness, provider_qos=value)
    assert exc.value.reason == "invalid-args"
    assert daemon.store.query("SELECT job_id FROM jobs") == []


def test_d_qos_booked1_the_request_is_part_of_the_payload(launch_state):  # noqa: F811
    """C-6.2: one request id is one request; a retry asking another QoS is another payload."""
    daemon, harness, _ = launch_state
    args = harness.submit_args(request_id="booked-run", provider_qos="default")
    first = daemon.dispatch("submit", args)
    assert daemon.dispatch("submit", args)["job_id"] == first["job_id"]
    with pytest.raises(daemon_module.protocol.ProtocolError) as exc:
        daemon.dispatch("submit", {**args, "provider_qos": None})
    assert exc.value.reason == "different-payload"
    # `utility` and no request are one request: the clamp.
    plain = harness.submit_args(request_id="plain-run")
    job_id = daemon.dispatch("submit", plain)["job_id"]
    assert daemon.dispatch("submit", {**plain, "provider_qos": "utility"})["job_id"] == job_id


def test_d_qos_booked1_runs_show_carries_the_request(launch_state):  # noqa: F811
    daemon, harness, _ = launch_state
    asked = submit(daemon, harness, provider_qos="default")
    plain = submit(daemon, harness)
    assert daemon.dispatch("show", {"job_id": asked})["job"]["provider_qos"] == "default"
    assert daemon.dispatch("show", {"job_id": plain})["job"]["provider_qos"] == "utility"
    assert daemon.store.get_job(asked)["provider_qos"] == "default"
    # `runs` reads every job as `show` does (#277 review r1, P2); raw SQL keeps NULL.
    listed = {row["job_id"]: row["provider_qos"] for row in daemon.dispatch("list", {})["jobs"]}
    assert listed == {asked: "default", plain: "utility"}
    assert daemon.store.one("SELECT provider_qos FROM jobs WHERE job_id=?", (plain,))["provider_qos"] is None


def test_d_qos_booked1_why_says_an_unhonoured_request_and_why(launch_state):  # noqa: F811
    daemon, harness, _ = launch_state
    daemon.policy["admission"]["priority_callers"] = [CHOSEN]
    job_id = submit(daemon, harness, caller_session=OTHER, provider_qos="default")
    why = daemon.dispatch("why", {"job_id": job_id})
    reason = "qos default requested; caller is not in admission.priority_callers; provider clamped to utility"
    assert reason in why["queue"] and reason in why["text"].splitlines()
    assert why["job"]["qos"] == {"requested": "default", "provider_qos": "utility", "reason": reason,
                                 "started": None}


def test_d_qos_booked1_why_names_what_the_provider_was_given(priority):
    daemon, harness, calls = priority
    job_id = submit(daemon, harness, caller_session=CHOSEN, provider_qos="default")
    attempt, command = launch_first(daemon, calls, job_id)
    assert asks_inherit(command)
    # The guardian's receipt, as one that ran would have left it.
    atomic_publish(attempt_dir(daemon.root, job_id, 1) / "start.json", json.dumps(
        {"guardian_pid": 987654321, "pgid": 987654321, "boot_id": "fixture-boot", "proc_start": "fixture-start",
         "started_at": "2026-10-28T00:00:00.000Z", "provider_qos": "inherit"}).encode())
    why = daemon.dispatch("why", {"job_id": job_id})
    assert why["job"]["qos"]["started"] == {"seq": 1, "provider_qos": "inherit"}
    lines = why["text"].splitlines()
    assert ("qos default requested; class priority (admission.priority_callers); provider starts unclamped"
            in lines)
    assert "a1's provider started at inherit (start.json)" in lines
    # A utility job's why gains nothing (the clamp is every job's, C-5.1).
    plain = submit(daemon, harness, caller_session=CHOSEN)
    assert not [line for line in daemon.dispatch("why", {"job_id": plain})["text"].splitlines() if "qos" in line]
    assert daemon.dispatch("why", {"job_id": plain})["job"]["qos"]["requested"] == "utility"
