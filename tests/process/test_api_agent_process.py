"""The harness as its own process, against the local fake API (C-31.7 to C-31.16).

Each test launches `python -I -m subfleet.api_agent` with the interface's argv and a
recognizable fake key, and reads what the fake received and what the harness wrote.
No request leaves 127.0.0.1.

Invariant 2 (no identity prompt), end to end: for any job prompt and any effort,
sandbox and network setting, every request's `system` is the constant prompt for
that setting and holds no identity line, "Claude Code" or "Agent SDK" after case
folding and whitespace normalization (Hypothesis, a few launches per run).

Invariant 6 (the key stays in the harness): the key, and every 12-character run of
it, appear in none of the tool subprocess environments, the hook's environment,
`stream.jsonl`, `last.md`, stdout, stderr, `messages.jsonl`, any other file in the
attempt folder, or any request body; it reaches the fake only as the auth header.
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import HealthCheck, Phase, given, settings, strategies as st

from subfleet import home_paths
from subfleet.api_agent import cli, prompt
from subfleet.api_agent.loop import UNKNOWN_TEXT
from tests.fake.anthropic_api import (CREDIT_MESSAGE, DEFAULT_ORG, WORKSPACE_MESSAGE, Error, FakeAnthropic,
                                      Reply, StreamError, bash, text, thinking, tool_use)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the shell's sandbox is macOS seatbelt")

KEY = "sk-ant-api03-SUBFLEETFAKEKEY" + secrets.token_urlsafe(48) + "-fakeAA"
RUN = 12
TIMEOUT_S = 600


def runs(secret: str) -> list[str]:
    return [secret[index:index + RUN] for index in range(len(secret) - RUN + 1)]


def assert_no_key(label: str, data: str | bytes) -> None:
    """A failure names the record and counts the runs; it never prints them."""
    text_ = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    whole = KEY in text_
    assert not whole, f"the key is in {label}"
    leaked = sum(run in text_ for run in runs(KEY))
    assert not leaked, f"{leaked} runs of the key are in {label}"


GUARD = """#!/bin/bash
input=$(cat)
printf '%s\\n' "$input" >> "{calls}"
env > "{env_dump}"
case "$input" in
  *'git stash'*) printf '%s' '{{"hookSpecificOutput":{{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"[stash-shared] fixture denial"}}}}' ;;
esac
exit 0
"""
PUBLISH = """#!/bin/bash
input=$(cat)
case "$input" in
  *'git push'*) printf '%s' '{"hookSpecificOutput":{"permissionDecision":"deny","permissionDecisionReason":"[publish] review jobs do not publish"}}' ;;
esac
exit 0
"""


@dataclass
class Run:
    proc: subprocess.CompletedProcess
    attempt: Path
    work: Path
    root: Path

    @property
    def events(self) -> list[dict]:
        return [json.loads(line) for line in (self.attempt / "stream.jsonl").read_text().splitlines()]

    def kinds(self) -> list[str]:
        return [event["type"] for event in self.events]

    def end(self) -> dict:
        """The last launch's `end`: each launch in this attempt folder (a resume reuses
        it here) appends one `start` and exactly one `end`, in that order."""
        kinds = self.kinds()
        assert kinds.count("end") == kinds.count("start") >= 1, kinds
        assert kinds[-1] == "end", kinds
        return self.events[-1]

    @property
    def messages(self) -> list[dict]:
        return [json.loads(line) for line in (self.attempt / "messages.jsonl").read_text().splitlines()]


def script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def prepare(root: Path, fake: FakeAnthropic, *, job: str = "Do the job.", sandbox: str = "workspace-write",
            guard: bool = True, publish: bool = False, network: bool = False, max_usd: float = 25,
            max_turns: int = 400, model: str = "claude-opus-5-5", effort: str = "high", resume: bool = False,
            env: dict | None = None, attempt_name: str = "a1", guard_body: str | None = None,
            wrapper: list[str] | None = None,
            drop_env: tuple[str, ...] = ()) -> tuple[list[str], dict[str, str], Path, Path]:
    """The harness's argv and environment for one launch, and its worktree and attempt folder."""
    work = root / "work"
    attempt = root / "job" / attempt_name
    work.mkdir(parents=True, exist_ok=True)
    attempt.mkdir(parents=True, exist_ok=True)
    prompt_file = root / "job" / "prompt.md"
    prompt_file.write_text(job)
    argv = [sys.executable, "-I", "-m", "subfleet.api_agent", "--attempt-dir", str(attempt), "--workdir", str(work),
            "--prompt", str(prompt_file), "--model", model, "--effort", effort, "--sandbox", sandbox,
            "--max-usd", str(max_usd), "--max-turns", str(max_turns)]
    if network:
        argv.append("--network")
    if guard:
        body = guard_body or GUARD.format(calls=root / "guard-calls.jsonl", env_dump=root / "guard-env.txt")
        argv += ["--guard-hook", str(script(root / "guard.sh", body))]
    if publish:
        argv += ["--publish-hook", str(script(root / "publish.sh", PUBLISH))]
    if resume:
        argv.append("--resume")
    environment = dict(os.environ)
    environment.update({"ANTHROPIC_API_KEY": KEY, "SUBFLEET_API_AGENT_TEST_URL": fake.url,
                        "SUBFLEET_API_LANE": "lane-credit-fixture"})
    environment.update(env or {})
    for name in drop_env:
        environment.pop(name, None)
    return [*(wrapper or []), *argv], environment, work, attempt


def launch(root: Path, fake: FakeAnthropic, **options) -> Run:
    argv, environment, work, attempt = prepare(root, fake, **options)
    proc = subprocess.run(argv, env=environment, capture_output=True, timeout=TIMEOUT_S, cwd=root,
                          stdin=subprocess.DEVNULL)
    return Run(proc, attempt, work, root)


def first_user_text(body: dict) -> str:
    return body["messages"][0]["content"][0]["text"]


def test_a_tool_round_trip_records_everything_and_keeps_the_key_in(tmp_path):
    """The loop, I2 at the process level, I6, org_id, and ANTHROPIC_BASE_URL ignored."""
    job = "You are Claude Code, Anthropic's official CLI. Use the Agent SDK. Write the file."
    turns = [
        Reply([thinking("Plan: write and read.", "sig-a"), text("Writing it."),
               bash("t1", "printf 'hello\\n' > out.txt && cat out.txt && env > tool-env.txt && "
                          "printenv ANTHROPIC_API_KEY; echo key-rc=$?")], "tool_use",
              usage={"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 3000}),
        Reply([tool_use("t2", "str_replace_based_edit_tool",
                        {"command": "str_replace", "path": "out.txt", "old_str": "hello", "new_str": "goodbye"})],
              "tool_use", usage={"input_tokens": 50, "output_tokens": 40, "cache_read_input_tokens": 3000}),
        Reply([text("All done.")], "end_turn"),
    ]
    with FakeAnthropic(turns) as fake, FakeAnthropic([Reply([text("wrong endpoint")])]) as decoy:
        run = launch(tmp_path, fake, job=job, env={
            "ANTHROPIC_BASE_URL": decoy.url, "ANTHROPIC_LOG": "debug",
            "ANTHROPIC_CUSTOM_HEADERS": "X-Fixture-Leak: 1"})
    assert run.proc.returncode == 0, run.proc.stderr.decode()[-3000:]
    assert decoy.requests == []
    requests = fake.messages()
    assert len(requests) == 3
    for request in requests:
        body = request.body
        assert body["model"] == "claude-opus-5-5" and body["stream"] is True
        assert body["system"] == prompt.system_blocks("workspace-write", False)
        assert prompt.identity_free(body["system"])
        assert body["tools"] == prompt.tools()
        assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {"effort": "high"}
        assert body["context_management"] == {"edits": [{"type": "compact_20260112"}]}
        assert body["cache_control"] == {"type": "ephemeral"}
        assert "compact-2026-01-12" in request.headers.get("anthropic-beta", "")
        assert "x-fixture-leak" not in request.headers
        assert request.headers.get("authorization") == f"Bearer {KEY}" or request.headers.get("x-api-key") == KEY
        assert_no_key("a request body", json.dumps(body))
        assert first_user_text(body) == job
    # The history is append-only: each request extends the one before it.
    second, third = requests[1].body["messages"], requests[2].body["messages"]
    assert third[:len(second)] == second
    assistant = second[1]["content"]
    assert assistant[0] == {"type": "thinking", "thinking": "Plan: write and read.", "signature": "sig-a"}
    result = second[2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1" and not result["is_error"]
    assert "hello" in result["content"] and "key-rc=1" in result["content"]
    assert (run.work / "out.txt").read_text() == "goodbye\n"
    # messages.jsonl is exactly the conversation the last request carried, then the reply.
    assert run.messages == third + [{"role": "assistant", "content": [{"type": "text", "text": "All done.",
                                                                       "citations": None}]}] \
        or run.messages[:len(third)] == third
    assert (run.attempt / "last.md").read_text() == "All done."
    assert run.kinds() == ["start", "response", "tool", "response", "tool", "response", "end"]
    start = run.events[0]
    assert start["v"] == 1 and start["model"] == "claude-opus-5-5" and start["lane"] == "lane-credit-fixture"
    assert start["effort"] == "high" and start["sandbox"] == "workspace-write"
    responses = [event for event in run.events if event["type"] == "response"]
    assert [event["request_id"] for event in responses] == ["req_fake_0001", "req_fake_0002", "req_fake_0003"]
    assert all(event["org_id"] == DEFAULT_ORG and event["model"] == "claude-opus-5-5" for event in responses)
    assert responses[0]["usage"] == {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 3000}
    assert responses[0]["usd"] == pytest.approx((1000 * 4 + 200 * 20 + 3000 * 4 * 1.25) / 1e6)
    tools = [event for event in run.events if event["type"] == "tool"]
    assert tools[0] == {**tools[0], "name": "bash", "rc": 0, "denied": False, "reason": None}
    assert tools[1]["name"] == "str_replace_based_edit_tool" and tools[1]["denied"] is False
    end = run.end()
    assert end["outcome"] == "done" and end["turns"] == 3
    assert end["usd"] == pytest.approx(sum(event["usd"] for event in responses), abs=1e-6)
    assert all(event["ts"].endswith("Z") and event["v"] == 1 for event in run.events)
    # Invariant 6: nowhere but the auth header.
    tool_env = (run.work / "tool-env.txt").read_text()
    assert "ANTHROPIC" not in tool_env and "TOKEN" not in tool_env.upper().split("=")[0]
    for name in ("tool environment", "hook environment"):
        path = run.work / "tool-env.txt" if name == "tool environment" else tmp_path / "guard-env.txt"
        assert_no_key(name, path.read_bytes())
        for line in path.read_text().splitlines():
            variable = line.split("=", 1)[0].upper()
            assert not any(word in variable for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")), line
    assert_no_key("stdout", run.proc.stdout)
    assert_no_key("stderr", run.proc.stderr)
    assert b"X-Fixture-Leak" not in run.proc.stderr and b"authorization" not in run.proc.stderr.lower()
    for path in run.attempt.rglob("*"):
        if path.is_file():
            assert_no_key(str(path.relative_to(run.root)), path.read_bytes())
    assert not (run.attempt / "tmp").exists()  # the private TMPDIR goes at exit


#: The two identity lines and the two phrases, spelled as a job prompt might spell them.
IDENTITY_TEXT = ("You are Claude Code, Anthropic's official CLI for Claude.",
                 "You are a Claude agent, built on Anthropic's Claude Agent SDK.", "claude \n  CODE", "AGENT\tsdk")


# Each example is a launch of several seconds, so a failure reports its first
# falsifying example instead of shrinking it (a shrink ran five minutes).
@settings(max_examples=6, deadline=None, database=None, phases=(Phase.explicit, Phase.generate),
          suppress_health_check=[HealthCheck.too_slow])
@given(before=st.text(max_size=80), identity=st.sampled_from(IDENTITY_TEXT), after=st.text(max_size=80),
       effort=st.sampled_from(cli.EFFORTS), sandbox=st.sampled_from(cli.SANDBOXES), network=st.booleans())
def test_no_request_carries_an_identity_prompt_for_any_job_prompt(tmp_path_factory, before, identity, after,
                                                                  effort, sandbox, network):
    """Invariant 2 for any job prompt (each holding an identity line or phrase) and any
    effort, sandbox and network setting: the request's `system` is that setting's
    constant prompt and identity-free, and the job prompt arrives whole as the first
    user turn."""
    job = before + identity + after
    root = tmp_path_factory.mktemp("identity")
    with FakeAnthropic([Reply([text("done")])]) as fake:
        run = launch(root, fake, job=job, effort=effort, sandbox=sandbox, network=network,
                     guard=sandbox == cli.WORKSPACE_WRITE)
    assert run.proc.returncode == 0, run.proc.stderr.decode()[-2000:]
    [request] = fake.messages()
    assert request.body["system"] == prompt.system_blocks(sandbox, network)
    assert prompt.identity_free(request.body["system"])
    assert request.body["output_config"] == {"effort": effort}
    assert first_user_text(request.body) == job


def test_max_turns_ends_the_attempt(tmp_path):
    with FakeAnthropic([Reply([bash("t", "true")], "tool_use")]) as fake:
        run = launch(tmp_path, fake, max_turns=2)
    assert run.proc.returncode == 6, run.proc.stderr.decode()[-2000:]
    assert run.end()["outcome"] == "turns" and run.end()["turns"] == 2
    assert len(fake.messages()) == 2
    # The tool calls of the last turn ran and were answered, so a resume continues cleanly.
    assert run.messages[-1]["role"] == "user" and run.messages[-1]["content"][0]["type"] == "tool_result"


def test_max_usd_ends_the_attempt(tmp_path):
    expensive = Reply([bash("t", "true")], "tool_use", usage={"output_tokens": 1_000_000})
    with FakeAnthropic([expensive]) as fake:
        run = launch(tmp_path, fake, max_usd=5)
    assert run.proc.returncode == 6, run.proc.stderr.decode()[-2000:]
    end = run.end()
    assert end["outcome"] == "budget" and end["turns"] == 1 and end["usd"] >= 20
    assert len(fake.messages()) == 1
    with FakeAnthropic([expensive]) as fake:
        nothing = launch(tmp_path / "zero", fake, max_usd=0)
    assert nothing.proc.returncode == 6 and nothing.end()["outcome"] == "budget"
    assert fake.requests == []


def credit_script(check):
    def answer(body, index):
        if body.get("model") == "claude-haiku-5-5" and body.get("max_tokens") == 1:
            return check
        return Error(400, CREDIT_MESSAGE)
    return answer


@pytest.mark.parametrize("check,code,verdict,status", [
    (Error(400, CREDIT_MESSAGE), 3, "exhausted", 400),
    (Reply([text("o")], "max_tokens", model="claude-haiku-5-5"), 4, "refused-by-kind", 200),
])
def test_credit_refusals_run_the_neutral_check(tmp_path, check, code, verdict, status):
    with FakeAnthropic(credit_script(check)) as fake:
        run = launch(tmp_path, fake)
    assert run.proc.returncode == code, run.proc.stderr.decode()[-2000:]
    assert len(fake.requests) == 2
    neutral = fake.requests[1].body
    assert neutral == {"model": "claude-haiku-5-5", "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}]}
    kinds = run.kinds()
    assert kinds == ["start", "error", "credit", "end"]
    error = run.events[1]
    assert error["status"] == 400 and error["error_type"] == "invalid_request_error"
    assert "credit balance is too low" in error["message"]
    credit = run.events[2]
    assert credit["verdict"] == verdict and credit["check_status"] == status
    assert run.end()["outcome"] == ("credit" if verdict == "exhausted" else "refused-by-kind")


def test_a_workspace_limit_ends_without_a_check(tmp_path):
    with FakeAnthropic([Error(400, WORKSPACE_MESSAGE)]) as fake:
        run = launch(tmp_path, fake)
    assert run.proc.returncode == 5, run.proc.stderr.decode()[-2000:]
    assert len(fake.requests) == 1
    assert run.kinds() == ["start", "error", "credit", "end"]
    assert run.events[2]["verdict"] == "workspace-limit" and run.events[2]["check_status"] is None
    assert run.end()["outcome"] == "workspace-limit"


def test_an_inconclusive_check_is_an_error(tmp_path):
    with FakeAnthropic(credit_script(Error(401, "invalid x-api-key", "authentication_error"))) as fake:
        run = launch(tmp_path, fake)
    assert run.proc.returncode == 1
    assert "credit" not in run.kinds() and run.end()["outcome"] == "error"


def test_resume_continues_from_messages_jsonl(tmp_path):
    with FakeAnthropic([Reply([bash("t1", "echo first > first.txt")], "tool_use")]) as fake:
        first = launch(tmp_path, fake, max_turns=1)
    assert first.proc.returncode == 6 and (first.work / "first.txt").exists()
    before = (first.attempt / "messages.jsonl").read_bytes()
    with FakeAnthropic([Reply([text("Resumed and done.")])]) as fake:
        second = launch(tmp_path, fake, resume=True)
    assert second.proc.returncode == 0, second.proc.stderr.decode()[-2000:]
    [request] = fake.messages()
    assert request.body["messages"] == [json.loads(line) for line in before.splitlines()]
    after = (second.attempt / "messages.jsonl").read_bytes()
    assert after.startswith(before)  # append-only across the resume
    assert second.kinds().count("start") == 2 and second.end()["outcome"] == "done"


def test_resume_answers_a_call_that_never_got_its_result(tmp_path):
    attempt = tmp_path / "job" / "a1"
    attempt.mkdir(parents=True)
    history = [{"role": "user", "content": [{"type": "text", "text": "Do the job."}]},
               {"role": "assistant", "content": [{"type": "tool_use", "id": "t9", "name": "bash",
                                                  "input": {"command": "make"}}]}]
    (attempt / "messages.jsonl").write_text("".join(json.dumps(message) + "\n" for message in history)
                                            + '{"role": "us')  # a torn last write
    with FakeAnthropic([Reply([text("ok")])]) as fake:
        run = launch(tmp_path, fake, resume=True)
    assert run.proc.returncode == 0, run.proc.stderr.decode()[-2000:]
    sent = fake.messages()[0].body["messages"]
    assert sent[:2] == history
    assert sent[2]["content"] == [{"type": "tool_result", "tool_use_id": "t9", "content": UNKNOWN_TEXT,
                                   "is_error": True}]


def test_a_fresh_launch_refuses_an_existing_conversation(tmp_path):
    attempt = tmp_path / "job" / "a1"
    attempt.mkdir(parents=True)
    (attempt / "messages.jsonl").write_text(json.dumps({"role": "user", "content": "x"}) + "\n")
    with FakeAnthropic([Reply([text("ok")])]) as fake:
        run = launch(tmp_path, fake)
    assert run.proc.returncode == 1 and fake.requests == []
    assert run.end()["outcome"] == "error"


def test_org_id_is_null_without_the_header(tmp_path):
    with FakeAnthropic([Reply([text("done")])], org_id=None) as fake:
        run = launch(tmp_path, fake, sandbox="read-only", guard=False)
    assert run.proc.returncode == 0, run.proc.stderr.decode()[-2000:]
    [response] = [event for event in run.events if event["type"] == "response"]
    assert response["org_id"] is None


def test_a_mid_stream_overload_is_retried(tmp_path):
    with FakeAnthropic([StreamError(), Reply([text("done")])]) as fake:
        run = launch(tmp_path, fake)
    assert run.proc.returncode == 0, run.proc.stderr.decode()[-2000:]
    assert len(fake.messages()) == 2
    error = run.events[1]
    assert error["type"] == "error" and error["status"] is None and error["error_type"] == "overloaded_error"
    assert run.end()["turns"] == 1


def test_the_guard_and_the_publish_hook_deny_commands(tmp_path):
    turns = [Reply([bash("t1", "touch ran-stash.txt; git stash"), bash("t2", "touch ran-push.txt; git push"),
                    bash("t3", "touch ran-ok.txt")], "tool_use"),
             Reply([text("done")])]
    with FakeAnthropic(turns) as fake:
        run = launch(tmp_path, fake, publish=True)
    assert run.proc.returncode == 0, run.proc.stderr.decode()[-2000:]
    results = fake.messages()[1].body["messages"][2]["content"]
    assert [result["is_error"] for result in results] == [True, True, False]
    assert "[stash-shared] fixture denial" in results[0]["content"]
    assert "[publish] review jobs do not publish" in results[1]["content"]
    assert not (run.work / "ran-stash.txt").exists() and not (run.work / "ran-push.txt").exists()
    assert (run.work / "ran-ok.txt").exists()
    tools = [event for event in run.events if event["type"] == "tool"]
    assert [(event["denied"], event["rc"]) for event in tools] == [(True, None), (True, None), (False, 0)]
    assert "fixture denial" in tools[0]["reason"]
    calls = [json.loads(line) for line in (tmp_path / "guard-calls.jsonl").read_text().splitlines()]
    assert calls[0]["tool_name"] == "Bash" and calls[0]["tool_input"]["command"].endswith("git stash")
    assert calls[0]["cwd"] == os.path.realpath(run.work)


def test_a_writable_launch_needs_the_guard(tmp_path):
    with FakeAnthropic([Reply([text("ok")])]) as fake:
        run = launch(tmp_path, fake, guard=False)
    assert run.proc.returncode == 1 and fake.requests == []
    assert run.events[1]["error_type"] == "guard-required"


def test_read_only_jobs_write_nothing_in_the_worktree(tmp_path):
    turns = [Reply([bash("t1", "touch made.txt; echo rc=$?; touch \"$TMPDIR/scratch\" && echo tmp-ok"),
                    tool_use("t2", "str_replace_based_edit_tool",
                             {"command": "create", "path": "made2.txt", "file_text": "x"})], "tool_use"),
             Reply([text("done")])]
    with FakeAnthropic(turns) as fake:
        run = launch(tmp_path, fake, sandbox="read-only", guard=False)
    assert run.proc.returncode == 0, run.proc.stderr.decode()[-2000:]
    results = fake.messages()[1].body["messages"][2]["content"]
    assert "rc=1" in results[0]["content"] and "tmp-ok" in results[0]["content"]
    assert results[1]["is_error"]
    assert not (run.work / "made.txt").exists() and not (run.work / "made2.txt").exists()
    assert fake.messages()[0].body["system"] == prompt.system_blocks("read-only", False)


# Every session marker `under_pytest` reads (subfleet/home_paths.py).
SESSION_MARKERS = (home_paths.PYTEST_MARKER, home_paths.PYTEST_SESSION, home_paths.PYTEST_SESSION_ID,
                   "PYTEST_CURRENT_TEST")
#: Loopback only: no name resolution (mDNSResponder is a unix socket) and no remote IP.
LOOPBACK_ONLY = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
"""
RESOLVE = "import socket\ntry:\n    socket.getaddrinfo('api.anthropic.com', 443)\n    print('resolved')\n" \
          "except OSError:\n    print('refused')\n"


def test_outside_a_test_session_neither_the_override_nor_anthropic_base_url_is_used(tmp_path):
    """C-31.11: without a pytest session's markers, the harness honors neither the test
    override nor ANTHROPIC_BASE_URL, and sends to https://api.anthropic.com. The
    harness runs under a loopback-only seatbelt profile, checked first, so those
    requests fail on this host and none leaves it; both local fakes see nothing."""
    profile = tmp_path / "loopback-only.sb"
    profile.write_text(LOOPBACK_ONLY)
    wrapper = ["/usr/bin/sandbox-exec", "-f", str(profile)]
    probe = subprocess.run([*wrapper, sys.executable, "-I", "-c", RESOLVE], capture_output=True, text=True,
                           timeout=120)
    assert probe.stdout.strip() == "refused", (probe.stdout, probe.stderr)
    with FakeAnthropic([Reply([text("the override")])]) as fake, \
            FakeAnthropic([Reply([text("ANTHROPIC_BASE_URL")])]) as decoy:
        child = {**os.environ, "SUBFLEET_API_AGENT_TEST_URL": fake.url, "ANTHROPIC_BASE_URL": decoy.url}
        for name in SESSION_MARKERS:
            child.pop(name, None)
        assert not home_paths.under_pytest(child)
        run = launch(tmp_path, fake, env={"ANTHROPIC_BASE_URL": decoy.url}, wrapper=wrapper,
                     drop_env=SESSION_MARKERS)
    assert fake.requests == [] and decoy.requests == []
    assert run.proc.returncode == 1, run.proc.stderr.decode()[-2000:]
    errors = [event for event in run.events if event["type"] == "error"]
    assert errors and all(event["error_type"] in ("connection", "timeout") for event in errors), errors
    assert run.end()["outcome"] == "error" and run.end()["turns"] == 0
    assert_no_key("stderr", run.proc.stderr)


PROCARGS_DUMP = """import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
size = ctypes.c_size_t(os.sysconf("SC_ARG_MAX"))
buffer = ctypes.create_string_buffer(size.value)
name = (ctypes.c_int * 3)(1, 49, int(sys.argv[1]))  # CTL_KERN, KERN_PROCARGS2, pid
if libc.sysctl(name, 3, buffer, ctypes.byref(size), None, 0) != 0:
    sys.exit(f"sysctl failed: errno {ctypes.get_errno()}")
open(sys.argv[2], "wb").write(buffer.raw[:size.value])
print("dumped", size.value)
"""
PS_GUARD = """#!/bin/bash
cat >/dev/null
/bin/ps -Eww -p "$PPID" >> "{dump}"
exit 0
"""


def test_no_process_reads_the_key_from_the_harness_exec_environment(tmp_path):
    """Invariant 6, C-31.12: popping the key from os.environ leaves it in the block
    the kernel copied at exec, which any process of the same user reads with
    `sysctl(KERN_PROCARGS2)` (seatbelt does not stop it) or `ps -E`. The harness
    overwrites it there: a tool inside the sandbox and the guard hook outside it
    each read the harness's block and find the variable, but neither the key nor
    any 12-character run of it."""
    reader = tmp_path / "procargs_dump.py"
    reader.write_text(PROCARGS_DUMP)
    command = f"{shlex.quote(sys.executable)} -I {shlex.quote(str(reader))} $PPID harness-procargs.bin"
    turns = [Reply([bash("t1", command)], "tool_use"), Reply([text("done")])]
    with FakeAnthropic(turns) as fake:
        run = launch(tmp_path, fake, guard_body=PS_GUARD.format(dump=tmp_path / "harness-ps.txt"))
    assert run.proc.returncode == 0, run.proc.stderr.decode()[-2000:]
    result = fake.messages()[1].body["messages"][2]["content"][0]
    assert "dumped" in result["content"], result
    block = (run.work / "harness-procargs.bin").read_bytes()
    listed = (tmp_path / "harness-ps.txt").read_text()
    for label, data in (("the sandboxed tool's KERN_PROCARGS2 read", block), ("ps -E from the guard hook", listed)):
        assert_no_key(label, data)
    # They read the right process: the variable is there, its value overwritten in place,
    # and the strings after it are whole.
    strings = block.split(b"\0")
    assert b"ANTHROPIC_API_KEY=" + b"*" * (len(KEY)) in strings
    assert b"SUBFLEET_API_LANE=lane-credit-fixture" in strings
    assert "ANTHROPIC_API_KEY=" + "*" * len(KEY) in listed


CREDENTIAL_FILES = (".ssh/id_ed25519", ".codex/auth.json", ".codex-3/auth.json", ".codex-api/auth.json",
                    ".config/gh/hosts.yml", ".netrc", ".git-credentials", ".claude.json",
                    ".claude/.credentials.json", ".config/claude-credits/registry.json", ".aws/credentials",
                    ".docker/config.json", ".npmrc", ".pypirc", "Library/Keychains/login.keychain-db")


def test_the_shell_cannot_read_credential_stores(tmp_path):
    """C-31.13: the shell reads anywhere but the account's keychains and credential
    stores, in each spelling of the home (here a fixture HOME): Codex's home and the
    lane homes beside it, SSH and GPG keys, and the usual token files. A plain file
    beside them stays readable."""
    home = tmp_path / "home"
    for relative in CREDENTIAL_FILES:
        (home / relative).parent.mkdir(parents=True, exist_ok=True)
        (home / relative).write_text("fixture-credential\n")
    (home / "notes.txt").write_text("plain notes\n")
    checks = "; ".join(f'cat "$HOME/{relative}" >/dev/null 2>&1 && echo "READ {relative}" || echo "refused {relative}"'
                       for relative in CREDENTIAL_FILES)
    turns = [Reply([bash("t1", checks + '; cat "$HOME/notes.txt"')], "tool_use"), Reply([text("done")])]
    with FakeAnthropic(turns) as fake:
        run = launch(tmp_path, fake, env={"HOME": str(home)})
    assert run.proc.returncode == 0, run.proc.stderr.decode()[-2000:]
    output = fake.messages()[1].body["messages"][2]["content"][0]["content"]
    assert "READ " not in output, output
    assert all(f"refused {relative}" in output for relative in CREDENTIAL_FILES), output
    assert "plain notes" in output


def test_sigterm_kills_the_shell_and_writes_no_end(tmp_path):
    """C-31.16: SIGTERM, the guardian's stop, ends the harness with exit 1 and no `end`
    event, after it kills the shell's process group (a background job included) and
    removes the private TMPDIR."""
    command = "sleep 300 & echo $! > sleep.pid; wait"
    with FakeAnthropic([Reply([bash("t1", command)], "tool_use")]) as fake:
        argv, environment, work, attempt = prepare(tmp_path, fake)
        proc = subprocess.Popen(argv, env=environment, cwd=tmp_path, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            pid_file = work / "sleep.pid"
            deadline = time.monotonic() + TIMEOUT_S
            while proc.poll() is None and not (pid_file.exists() and pid_file.read_text().strip()):
                assert time.monotonic() < deadline, "the tool call never started"
                time.sleep(0.2)
            assert proc.poll() is None, proc.communicate()[1].decode()[-2000:]
            sleeper = int(pid_file.read_text())
            assert (attempt / "tmp").is_dir()
            proc.send_signal(signal.SIGTERM)
            _, err = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    assert proc.returncode == 1, err.decode()[-2000:]
    kinds = [json.loads(line)["type"] for line in (attempt / "stream.jsonl").read_text().splitlines()]
    assert kinds == ["start", "response"], kinds
    assert not (attempt / "tmp").exists()
    deadline = time.monotonic() + 30
    while True:
        try:
            os.kill(sleeper, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, "the shell's background job outlived the harness"
        time.sleep(0.2)
    assert_no_key("stderr", err)
