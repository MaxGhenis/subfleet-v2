"""Differential: what the real harness writes, read by the adapter, against the fixtures.

The adapter's tests (`tests/unit/test_claude_api_adapter.py`) classify hand-written
fixtures (`tests/fixtures/claude_api/<case>/`). Here, for each of those scenarios, the
real harness runs against the local fake API with the launch the real adapter builds:
its argv, plus the key on a pipe (`--key-fd`, C-31.17) and the test URL. The adapter's
`classify`, `attest` and `deliverable` then read what the harness really wrote
(`stream.jsonl`, `last.md`, its exit code), and must decide what they decide from the
fixture: the outcome class and detail, the closure's scope, reason, clock and label, the
evidence the daemon acts on, the attestation and whether there is a deliverable. Each
event type both streams hold has the same fields with values of the same JSON types, and
the event kinds come in the same order.

`test_a_stopped_attempt_resumes_with_its_history_only_growing`: an attempt is stopped
partway (SIGTERM, as the guardian stops it), the adapter's `resume_launch` copies its
`messages.jsonl`, and the harness continues from it; every request's messages extend
the one before, across the resume.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from subfleet import credit_lane
from subfleet.adapters.claude_api import ClaudeApiAdapter
from subfleet.contracts import ExitInfo, Sandbox
from subfleet.api_agent.loop import UNKNOWN_TEXT
from tests.fake.anthropic_api import (CREDIT_MESSAGE, WORKSPACE_MESSAGE, Error, FakeAnthropic, Reply, bash,
                                      text)
from tests.process.test_api_agent_process import TIMEOUT_S, spawn
from tests.unit.test_claude_api_adapter import EMAIL, FIXTURES, KEY, MODEL, ORG, OTHER_ORG, classified, job, lane

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the shell's sandbox is macOS seatbelt")

TEST_URL_ENV = "SUBFLEET_API_AGENT_TEST_URL"


def renewal_day() -> int:
    """A renewal day that is never today, so a credit closure's clock is the reported
    renewal on any date the test runs, as it is for the fixtures' 2026-11-10."""
    return 1 if dt.date.today().day != 1 else 2


def real_adapter(**policy) -> ClaudeApiAdapter:
    registry = {"version": 1, "accounts": {EMAIL: {"fingerprint": credit_lane.fingerprint(KEY), "org_id": ORG,
                                                   "renews_day": renewal_day()}}}
    found = ClaudeApiAdapter(python=sys.executable, registry_reader=lambda: registry)
    found.credit_policy = {"max_usd_per_attempt": 25, "max_turns_per_attempt": 400, "denied_org_ids": [], **policy}
    found.credit_budget = {"credit_usd": 200.0, "ledger_usd": 0.0, "in_flight_usd": 0.0}
    return found


def environment(launch, url: str) -> dict[str, str]:
    """The launch's environment as the daemon builds it: env_add over the parent's, less
    env_remove and ANTHROPIC_API_KEY (C-31.2); the key itself goes on the pipe."""
    env = {name: value for name, value in os.environ.items()
           if name not in (*launch.env_remove, "ANTHROPIC_API_KEY")}
    env.update(launch.env_add)
    env[TEST_URL_ENV] = url
    return env


def credit_script(check):
    def answer(body, index):
        if body.get("model") == "claude-haiku-5-5" and body.get("max_tokens") == 1:
            return check
        return Error(400, CREDIT_MESSAGE)
    return answer


#: case: (the fake's script, the fake's organization, the policy's limits, the exit code).
SCENARIOS = {
    "done": ([Reply([bash("t1", "true")], "tool_use"), Reply([text("The answer.")])], ORG, {}, 0),
    "credit": (credit_script(Error(400, CREDIT_MESSAGE)), ORG, {}, 3),
    "refused-by-kind": (credit_script(Reply([text("o")], "max_tokens", model="claude-haiku-5-5")), ORG, {}, 4),
    "workspace-limit": ([Error(400, WORKSPACE_MESSAGE)], ORG, {}, 5),
    "turns": ([Reply([bash("t1", "true")], "tool_use")], ORG, {"max_turns_per_attempt": 1}, 6),
    "budget": ([Reply([text("partial"), bash("t1", "true")], "tool_use", usage={"input_tokens": 1_000_000})], ORG,
               {"max_usd_per_attempt": 1.0}, 6),
    "error-401": ([Error(401, "invalid x-api-key", "authentication_error")], ORG, {}, 1),
    "error-429": ([Error(429, "rate limited", "rate_limit_error")], ORG, {}, 1),
    "error": ([Error(500, "Internal", "api_error")], ORG, {}, 1),
    "other-model": ([Reply([text("The answer.")], model="claude-haiku-5-5")], ORG, {}, 0),
    "other-org": ([Reply([text("The answer.")])], OTHER_ORG, {}, 0),
    "no-end": ([Reply([bash("t1", 'echo running > "$TMPDIR/started"; sleep 300')], "tool_use")], ORG, {}, 1),
}


def run_harness(tmp_path: Path, case: str):
    """The real harness's run of `case`: its attempt folder, launch and exit code."""
    script, org, policy, _ = SCENARIOS[case]
    found = real_adapter(**policy)
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    spec = job(tmp_path)
    adir = tmp_path / "jobs" / "job1" / "a1"
    launch = found.build_launch(spec, "job1/a1", adir, lane(), {"ANTHROPIC_API_KEY": KEY, "TMPDIR": str(tmp_path)},
                                MODEL, "high", Path(spec.prompt_path), None)
    with FakeAnthropic(script, org_id=org) as fake, open(launch.stdout_path, "wb") as stdout, \
            open(launch.stderr_path, "wb") as stderr:
        proc = spawn(list(launch.argv), environment(launch, fake.url), tmp_path, key=KEY, stdout=stdout,
                     stderr=stderr)
        try:
            if case == "no-end":
                # The guardian's stop, once the tool is running (C-31.16).
                started = adir / "tmp" / "started"
                deadline = time.monotonic() + TIMEOUT_S
                while not started.exists():
                    assert proc.poll() is None and time.monotonic() < deadline, "the tool never started"
                    time.sleep(0.05)
                proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=TIMEOUT_S)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    return found, adir, launch, rc


def summary(found, adir, launch, outcome) -> dict:
    """What the daemon acts on from one classification (never a time or an amount)."""
    closure = outcome.closure
    evidence = outcome.evidence
    attestation = found.attest(adir, launch, outcome, MODEL)
    deliverable = found.deliverable(adir, launch, outcome)
    return {
        "class": outcome.cls.value, "detail": outcome.detail,
        "closure": None if closure is None else [closure.scope, closure.reason.value, closure.clock_source.value,
                                                 closure.source_event],
        "end": evidence["end"], "end_outcome": evidence["end_outcome"],
        "credit": evidence.get("credit"), "error": evidence.get("error"),
        "identity": (evidence.get("identity") or {}).get("status"),
        "alert": (evidence.get("alert") or {}).get("key"),
        "answered": evidence["model_answered"], "responded": evidence["responses"] > 0,
        "turns_type": type(evidence["turns"]).__name__,
        "usd_type": "number" if isinstance(evidence["usd"], (int, float)) else type(evidence["usd"]).__name__,
        "attestation": [attestation.status.value, attestation.served_model],
        "deliverable": None if deliverable is None else deliverable.decode().strip(),
    }


def kind(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def shapes(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    events = [json.loads(line) for line in path.read_text().splitlines()]
    found: dict[str, dict[str, str]] = {}
    for event in events:
        found.setdefault(event["type"], {}).update({key: kind(value) for key, value in event.items()})
    return [event["type"] for event in events], found


@pytest.mark.parametrize("case", sorted(SCENARIOS))
def test_the_adapter_reads_the_real_harness_as_it_reads_the_fixture(tmp_path, case):
    found, adir, launch, rc = run_harness(tmp_path / "real", case)
    assert rc == SCENARIOS[case][3], (adir / "stderr").read_text()[-2000:]
    real = summary(found, adir, launch, found.classify(adir, launch, ExitInfo(rc=rc, signal=None, wall_s=1.0,
                                                                                child_pid=None)))
    fixture_adapter, fixture_dir, fixture_launch, fixture_outcome = classified(tmp_path / "fixture", case, rc=rc)
    expected = summary(fixture_adapter, fixture_dir, fixture_launch, fixture_outcome)
    assert real == expected
    real_kinds, real_shapes = shapes(adir / "stream.jsonl")
    fixture_kinds, fixture_shapes = shapes(FIXTURES / case / "stream.jsonl")
    assert real_kinds == fixture_kinds
    assert real_shapes == fixture_shapes


def test_a_stopped_attempt_resumes_with_its_history_only_growing(tmp_path):
    """C-31.1, C-31.16: SIGTERM stops the first attempt during a tool call; the adapter's
    resume copies its history; the harness answers the call that never got its result
    and goes on, and every request's messages extend the one before, across the resume."""
    found = real_adapter()
    spec = job(tmp_path)
    (tmp_path / "work").mkdir()
    first, second = tmp_path / "jobs" / "job1" / "a1", tmp_path / "jobs" / "job1" / "a2"
    credential_env = {"ANTHROPIC_API_KEY": KEY, "TMPDIR": str(tmp_path)}
    launch = found.build_launch(spec, "job1/a1", first, lane(), credential_env, MODEL, "high",
                                Path(spec.prompt_path), None)
    turns = [Reply([bash("t1", "echo one")], "tool_use"),
             Reply([bash("t2", 'echo running > "$TMPDIR/started"; sleep 300')], "tool_use")]
    with FakeAnthropic(turns, org_id=ORG) as fake:
        proc = spawn(list(launch.argv), environment(launch, fake.url), tmp_path, key=KEY,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + TIMEOUT_S
            while not (first / "tmp" / "started").exists():
                assert proc.poll() is None and time.monotonic() < deadline, "the second tool call never started"
                time.sleep(0.05)
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=TIMEOUT_S) == 1
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        before_requests = [request.body["messages"] for request in fake.messages()]
    history = (first / "messages.jsonl").read_bytes()
    resumed = found.resume_launch(spec, "job1/a2", second, lane(), credential_env, "job1/a1",
                                  Path(spec.prompt_path), None, MODEL)
    assert resumed.argv[-1] == "--resume" and (second / "messages.jsonl").read_bytes() == history
    with FakeAnthropic([Reply([text("Resumed and done.")])], org_id=ORG) as fake:
        proc = spawn(list(resumed.argv), environment(resumed, fake.url), tmp_path, key=KEY,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert proc.wait(timeout=TIMEOUT_S) == 0
        after_requests = [request.body["messages"] for request in fake.messages()]
    saved = [json.loads(line) for line in history.splitlines()]
    sent = after_requests[0]
    # The history only grows: each request extends the one before, the first resumed one
    # extends the saved history, and the saved file is a prefix of the resumed one.
    requests = [*before_requests, *after_requests]
    assert all(later[:len(earlier)] == earlier for earlier, later in zip(requests, requests[1:])), \
        [len(messages) for messages in requests]
    assert sent[:len(saved)] == saved and saved[-1]["role"] == "assistant"
    assert sent[len(saved)]["content"] == [{"type": "tool_result", "tool_use_id": "t2", "content": UNKNOWN_TEXT,
                                            "is_error": True}]
    assert (second / "messages.jsonl").read_bytes().startswith(history)
    outcome = found.classify(second, resumed, ExitInfo(rc=0, signal=None, wall_s=1.0, child_pid=None))
    assert outcome.cls.value == "ok" and found.deliverable(second, resumed, outcome) == b"Resumed and done."
