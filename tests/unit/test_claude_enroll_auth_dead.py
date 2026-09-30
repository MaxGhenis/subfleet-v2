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


# --- the usage read enrolment makes (C-10.8, review of ae3455a1) ----------------

class Counting(Runner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.turns: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        if not (argv[0].endswith("security") or argv[1:2] == ["get"]):
            self.turns.append(list(argv))
        return super().__call__(argv, **kwargs)


def enrolling(tmp_path, usage_status, *, runner=None, body=b"{}"):
    return ClaudeAdapter(runner=runner or Runner(stream()), now=lambda: NOW, projects_dir=tmp_path,
                         profile_opener=profile_opener(403, b"{}"),
                         usage_opener=lambda request, timeout: (usage_status, body))


def test_c10_2_enrolment_refuses_a_token_the_usage_endpoint_refuses(tmp_path):
    """The probe cycle disables a lane whose usage read answers 401 (C-9.9). A
    lapsed setup token can still finish a turn: enrolment asks the usage endpoint
    with the same token and refuses, so a restore is never undone a minute later."""
    with pytest.raises(AdapterError) as caught:
        enrolling(tmp_path, 401).enroll(KEYCHAIN)
    assert caught.value.code == 5 and "usage endpoint refused" in str(caught.value)


@pytest.mark.parametrize("status,expected", [(403, "no-scope"), (429, "rate-limited"), (500, "unavailable")])
def test_c10_2_other_usage_answers_do_not_stop_enrolment(tmp_path, status, expected):
    info = enrolling(tmp_path, status).enroll(KEYCHAIN)
    assert info.usage_status == expected and info.identity_status == "enrolled"


def test_c10_8_enrolment_records_the_tokens_fingerprint_never_the_token(tmp_path):
    import hashlib
    info = enrolling(tmp_path, 403).enroll(KEYCHAIN)
    assert info.credential_fingerprint == hashlib.sha256(b"sk-ant-oat01-REDACTED").hexdigest()[:16]
    assert "REDACTED" not in json.dumps(info.__dict__, default=str)
    from subfleet.adapters.claude import credential_fingerprint
    assert credential_fingerprint({"CLAUDE_CONFIG_DIR": "/some/home"}) is None
    assert credential_fingerprint(None) is None


def test_c10_2_enrolment_is_one_haiku_turn(tmp_path):
    runner = Counting(stream())
    enrolling(tmp_path, 403, runner=runner).enroll(KEYCHAIN)
    assert len(runner.turns) == 1
    assert runner.turns[0][runner.turns[0].index("--model") + 1] == MODEL


def test_c10_2_a_stream_that_never_initialised_is_refused(tmp_path):
    with pytest.raises(AdapterError) as caught:
        adapter(tmp_path, stream(init=False)).enroll(KEYCHAIN)
    assert caught.value.code == 5


def test_c10_2_an_authenticated_account_at_its_limit_enrols(tmp_path):
    """A limit is not authentication: rc 1 with `system/init` and a usage-limit
    result is an account that authenticates, so it enrols (its closure is the
    probe cycle's to find)."""
    info = adapter(tmp_path, stream(result_error="Claude AI usage limit reached|1788624000"), rc=1).enroll(KEYCHAIN)
    assert info.account_key


# An oracle written out by hand, not derived from `auth_dead_evidence`: what C-9.3
# says each stream is. The differential above catches the two paths disagreeing;
# this catches them agreeing on something wrong.
ORACLE = [
    (dict(init=False), "API Error: 401 Unauthorized", True),
    (dict(init=False), "error: not logged in", True),
    (dict(init=False, text="does not have access to Claude"), "", True),
    (dict(init=True, text="Your organization has disabled Claude subscription access for Claude Code"), "", True),
    (dict(init=False, text="Your organization has disabled Claude subscription access for Claude Code"), "", True),
    (dict(init=True, error="oauth_org_not_allowed", text="x"), "", True),
    (dict(init=True, error="account_on_hold", text="x"), "", True),
    (dict(init=True, error="authentication_failed", text="x"), "", True),
    (dict(init=True, text="the tool got 401 Unauthorized"), "", False),
    (dict(init=True, error="overloaded", text="model at capacity"), "", False),
    (dict(init=True, error="rate_limit", text="x"), "", False),
    (dict(init=True, result_error="Claude AI usage limit reached|1788624000"), "", False),
    (dict(init=True), "", False),
]


@pytest.mark.parametrize("shape,stderr,dead", ORACLE)
def test_c9_3_classify_and_enrol_agree_with_the_written_oracle(tmp_path, shape, stderr, dead):
    stdout = stream(**shape)
    attempt = tmp_path / "a1"
    attempt.mkdir()
    (attempt / "stream.jsonl").write_text(stdout, encoding="utf-8")
    (attempt / "stderr").write_text(stderr, encoding="utf-8")
    launch = make_launch(attempt, session_id="s", model_id=MODEL)
    outcome = adapter(tmp_path, stdout).classify(attempt, launch, exit_info(1))
    assert (outcome.cls is OutcomeClass.AUTH_DEAD) is dead, outcome.detail
    if dead:
        with pytest.raises(AdapterError) as caught:
            adapter(tmp_path, stdout, stderr, 1).enroll(KEYCHAIN)
        assert caught.value.code == 5
