"""C-10.2: re-enrolling a keychain-token lane runs only its login turn through the fence.

2026-09-27, live: `subfleet lanes enroll claude-quota-max@axiom.org` for a lane
Subfleet had disabled as auth-dead failed with "Daemon._enrollment_turn() missing
2 required keyword-only arguments: 'cwd' and 'env'". The re-enrollment path sent
every adapter runner call through the enrollment fence, including the keychain
read that resolves the token before any turn, so no lapsed keychain-token account
could be brought back.
"""
from __future__ import annotations

import subprocess

import pytest

from subfleet.adapters.base import Credential
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.daemon import enrollment_runner
from tests.unit.test_claude_adapter import (
    FIXTURES, LANE_IDENTITY, NOW, _enroll_stream, _Runner, profile_opener,
)
import json


class Fence:
    """`Daemon._enrollment_turn`'s signature, recording what reaches it."""

    def __init__(self, completed):
        self.completed = completed
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, cwd, env, timeout, **_):
        self.calls.append(list(argv))
        return self.completed(argv)


def _adapter(tmp_path, runner):
    return ClaudeAdapter(runner=runner, now=lambda: NOW, projects_dir=tmp_path,
                         profile_opener=profile_opener())


def _info():
    return json.loads((FIXTURES / "success-allowed" / "stdout").read_text(encoding="utf-8")
                      .splitlines()[2])["rate_limit_info"]


KEYCHAIN = Credential(provider="claude", ref="claude-quota-max@axiom.org", kind="keychain-token")


def test_c10_2_a_keychain_token_reenrollment_reads_the_keychain_outside_the_fence(tmp_path):
    runner = _Runner(stdout=_enroll_stream(info=_info()))
    adapter = _adapter(tmp_path, runner)
    fence = Fence(lambda argv: subprocess.CompletedProcess(argv, 0, _enroll_stream(info=_info()), ""))
    adapter._runner = enrollment_runner(adapter._runner, fence)
    info = adapter.enroll(KEYCHAIN)
    assert info.identity == LANE_IDENTITY and info.identity_status == "verified"
    # The login turn, and only it, went through the fence; the keychain reads did not.
    assert len(fence.calls) == 1
    assert not any(call[0].endswith("security") or call[1:2] == ["get"] for call in fence.calls)
    assert any(call[0].endswith("security") or call[1:2] == ["get"] for call in runner.calls)


def test_c10_2_routing_every_call_through_the_fence_fails_as_it_did_live(tmp_path):
    """The wiring this replaces, kept as a reproduction: the keychain read reaches the
    fence without `cwd` and `env`, and the enroll never gets to its turn."""
    adapter = _adapter(tmp_path, _Runner(stdout=_enroll_stream(info=_info())))
    fence = Fence(lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))
    adapter._runner = lambda argv, **kwargs: fence(argv, **kwargs)
    with pytest.raises(TypeError, match="cwd"):
        adapter.enroll(KEYCHAIN)
    assert fence.calls == []


@pytest.mark.parametrize("argv,to_fence", [
    (["/Users/x/bin/agent-secret", "get", "claude-quota-max@axiom.org"], False),
    (["security", "find-generic-password", "-s", "claude-quota-max@axiom.org", "-w"], False),
    (["claude", "-p", "--model", "claude-haiku-4-5", "--output-format", "stream-json"], True),
    (["/Users/x/bin/agent-secret", "get", "a", "b"], True),       # not a shape keychain_command builds
    (["claude"], True),
])
def test_c10_2_the_router_fails_closed_everything_but_a_keychain_read_is_fenced(argv, to_fence):
    seen = []
    runner = enrollment_runner(lambda a, **kw: seen.append("original"),
                               lambda a, **kw: seen.append("fence"))
    runner(argv, capture_output=True)
    assert seen == ["fence" if to_fence else "original"]
