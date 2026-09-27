"""C-10.2, C-9.3, C-10.8: enrolment refuses what `classify` calls auth-dead.

Enrolment is how a lane comes back, by hand or by the auth-dead re-check (C-10.8).
It had looked only for a missing `system/init` and the organisation-block phrase,
while `classify` also calls the auth error kinds (`oauth_org_not_allowed`,
`account_on_hold`) auth-dead after `system/init`. A lapsed account answering that
way would have been re-enrolled, disabled by its first run, re-enrolled again at
the next re-check, and so on. Both now ask `auth_dead_evidence`; the differential
property below runs the real `enroll` and the real `classify` on the same streams.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile

from hypothesis import given, settings, strategies as st
import pytest

from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude import ClaudeAdapter, auth_dead_evidence
from subfleet.adapters.claude_stream import AUTH_ERROR_KINDS, parse_stream
from subfleet.contracts import Credential, OutcomeClass
from tests.conftest import NOW, exit_info, make_launch, profile_opener

KEYCHAIN = Credential(provider="claude", ref="claude-quota-max@axiom.org", kind="keychain-token")
MODEL = "claude-haiku-4-5-20251001"


class Runner:
    def __init__(self, stdout, stderr="", rc=0):
        self.stdout, self.stderr, self.rc = stdout, stderr, rc

    def __call__(self, argv, **kwargs):
        if argv[0].endswith("security") or argv[1:2] == ["get"]:
            return subprocess.CompletedProcess(argv, 0, "sk-ant-oat01-REDACTED", "")
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


def stream(*, init=True, error=None, text="ok", result_error=None):
    rows = []
    if init:
        rows.append({"type": "system", "subtype": "init", "session_id": "s", "model": MODEL})
    assistant = {"type": "assistant", "session_id": "s",
                 "message": {"model": MODEL, "content": [{"type": "text", "text": text}]}}
    if error:
        assistant["error"] = error
    rows.append(assistant)
    if result_error:
        rows.append({"type": "result", "subtype": "error_during_execution", "is_error": True,
                     "session_id": "s", "errors": [result_error]})
    else:
        rows.append({"type": "result", "subtype": "success", "is_error": False, "result": text,
                     "session_id": "s"})
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def adapter(tmp_path, stdout, stderr="", rc=0):
    return ClaudeAdapter(runner=Runner(stdout, stderr, rc), now=lambda: NOW, projects_dir=tmp_path,
                         profile_opener=profile_opener())


@pytest.mark.parametrize("kind", AUTH_ERROR_KINDS)
def test_c10_2_enrolment_refuses_an_auth_error_kind_after_system_init(tmp_path, kind):
    """The stream authenticated far enough to init, then the provider refused the
    account: that is `classify`'s auth-dead, so enrolment refuses it with exit 5."""
    with pytest.raises(AdapterError) as caught:
        adapter(tmp_path, stream(error=kind, text="Your account is not available")).enroll(KEYCHAIN)
    assert caught.value.code == 5
    assert kind in str(caught.value)
    assert "renew" in (caught.value.fix or "")


def test_c10_2_the_organisation_block_keeps_its_own_words(tmp_path):
    text = "Your organization has disabled Claude subscription access for Claude Code"
    with pytest.raises(AdapterError) as caught:
        adapter(tmp_path, stream(error="oauth_org_not_allowed", text=text), rc=1).enroll(KEYCHAIN)
    assert caught.value.code == 5
    assert "organisation has disabled" in str(caught.value)


def test_c10_2_a_401_after_system_init_is_not_auth_evidence(tmp_path):
    """C-9.3: after `system/init` a 401 is something else's; enrolment still succeeds."""
    info = adapter(tmp_path, stream(text="the tool got 401 Unauthorized from example.test")).enroll(KEYCHAIN)
    assert info.account_key and info.identity_status == "verified"


# --- the differential: enrol accepts => classify is not auth-dead --------------

TEXTS = ["ok", "Your organization has disabled Claude subscription access for Claude Code",
         "does not have access to Claude", "API Error: 401 Unauthorized", "OAuth token has expired",
         "Invalid API key", "please run /login", "Claude AI usage limit reached|1788624000",
         "model at capacity", "authentication_error"]
STDERRS = ["", "API Error: 401 Unauthorized", "error: not logged in", "Server error 500",
           "Your organization has disabled Claude subscription access"]
KINDS = [None, *AUTH_ERROR_KINDS, "overloaded", "server_error", "rate_limit", "invalid_request"]


@settings(max_examples=200, deadline=None)
@given(init=st.booleans(), kind=st.sampled_from(KINDS), text=st.sampled_from(TEXTS),
       stderr=st.sampled_from(STDERRS), result_error=st.one_of(st.none(), st.sampled_from(TEXTS)),
       rc=st.sampled_from([0, 1]))
def test_c10_2_enrolment_never_accepts_a_stream_classify_calls_auth_dead(init, kind, text, stderr,
                                                                          result_error, rc):
    stdout = stream(init=init, error=kind, text=text, result_error=result_error)
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        try:
            adapter(root, stdout, stderr, rc).enroll(KEYCHAIN)
            accepted = True
        except AdapterError:
            accepted = False
        attempt = root / "a1"
        attempt.mkdir()
        (attempt / "stream.jsonl").write_text(stdout, encoding="utf-8")
        (attempt / "stderr").write_text(stderr, encoding="utf-8")
        launch = make_launch(attempt, session_id="s", model_id=MODEL)
        outcome = adapter(root, stdout).classify(attempt, launch, exit_info(rc))
    corpus = "\n".join([stderr, *parse_stream(stdout).texts()])
    dead = auth_dead_evidence(parse_stream(stdout), corpus) is not None
    if accepted:
        assert outcome.cls is not OutcomeClass.AUTH_DEAD, outcome.detail
        assert not dead
    if dead:
        assert not accepted
