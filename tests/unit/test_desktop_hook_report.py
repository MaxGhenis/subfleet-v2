"""C-10.9: a desktop session's hook reports the app's account, and nothing else changes.

Every environment here is built by the test: none reads the session's own, and
every value is synthetic. The differential (invariant 5) runs release/217's own
`subfleet/hooks.py` at 266c3ab8b, frozen in `tests/fixtures/hooks_266c3ab8b.py`
and checked by its SHA-256, so it needs no git history.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import desktop_app, hooks
from subfleet.client import DaemonError, DaemonUnavailable, RequestTimeout, ResponseLost
from subfleet.protocol import ProtocolError

LAWS = settings(max_examples=250, deadline=None, derandomize=True, database=None)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BASE_HOOKS = FIXTURES / "hooks_266c3ab8b.py"
#: `git show 266c3ab8b:subfleet/hooks.py | shasum -a 256`
BASE_HOOKS_SHA256 = "f1f5b53be73fd77c7b5b75fda65ef06d4c28c5aa280beb978c3648615529b6ed"
SESSION = "sess-desktop-1"
EMAIL = "person@example.com"
ACCOUNT = "0000000c-0000-4000-8000-00000000000c"
ORG = "0000000a-0000-4000-8000-00000000000a"
REPORT_KEYS = {"email", "account_uuid", "org_uuid", "session_id", "observed_at"}


def load_base_hooks():
    data = BASE_HOOKS.read_bytes()
    assert hashlib.sha256(data).hexdigest() == BASE_HOOKS_SHA256
    name = "subfleet._hooks_266c3ab8b"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, BASE_HOOKS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def desktop_env(**extra) -> dict:
    return {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "CLAUDE_CODE_USER_EMAIL": EMAIL,
            "CLAUDE_CODE_ACCOUNT_UUID": ACCOUNT, "CLAUDE_CODE_ORGANIZATION_UUID": ORG, **extra}


def payload(event: str, **extra) -> dict:
    return {"session_id": SESSION, "hook_event_name": event, "cwd": "/repo", "transcript_path": "/dev/null", **extra}


class FakeClient:
    """Answers each op by a behaviour: a result, or an exception to raise."""

    def __init__(self, behaviours: dict, root: Path):
        self.behaviours, self.root, self.calls, self.timeout = behaviours, root, [], 30.0

    def call(self, op, args=None, *, timeout=None, retry_busy=None, request_id=""):
        self.calls.append((op, json.loads(json.dumps(args)), timeout))
        answer = self.behaviours.get(op, {})
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(args)
        return answer


@pytest.fixture(autouse=True)
def no_wake(monkeypatch):
    """The sessions kit's worker is not under test; neither hook module may spawn it."""
    monkeypatch.setattr(hooks, "wake_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(load_base_hooks(), "wake_worker", lambda *args, **kwargs: None)


def reports(client: FakeClient) -> list[dict]:
    return [args for op, args, _ in client.calls if op == desktop_app.REPORT_OP]


def run_event(module, event, body, root, client, env):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = module.session_event(event, body, root, client=client, stdout=stdout, env=env)
    return code, stdout.getvalue(), stderr.getvalue()


# --- invariant 4: only desktop sessions report, and only the three names ---------------

def test_c10_9_the_base_hooks_copy_is_266c3ab8b():
    assert hashlib.sha256(BASE_HOOKS.read_bytes()).hexdigest() == BASE_HOOKS_SHA256


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_c10_9_a_desktop_session_reports_its_account_once(event, tmp_path):
    client = FakeClient({"notice.pending": {"notices": []}, desktop_app.REPORT_OP: {"recorded": True}}, tmp_path)
    assert hooks.session_event(event, payload(event), tmp_path, client=client, stdout=io.StringIO(),
                               env=desktop_env()) == 0
    [report] = reports(client)
    assert set(report) == REPORT_KEYS
    assert (report["email"], report["account_uuid"], report["org_uuid"], report["session_id"]) == (
        EMAIL, ACCOUNT, ORG, SESSION)
    assert [timeout for op, _, timeout in client.calls if op == desktop_app.REPORT_OP] == [1.0]
    # One round trip, and before the notices are read.
    assert [op for op, _, _ in client.calls][:1] == [desktop_app.REPORT_OP]


@pytest.mark.parametrize("marker", ["SUBFLEET_ATTEMPT", "SUBFLEET_JOB"])
def test_c10_9_a_subfleet_launch_never_reports(marker, tmp_path):
    """Mutant 1's test: a launch marker means no report, whatever else the environment says."""
    client = FakeClient({"notice.pending": {"notices": []}}, tmp_path)
    for event in ("SessionStart", "UserPromptSubmit"):
        hooks.session_event(event, payload(event), tmp_path, client=client, stdout=io.StringIO(),
                            env=desktop_env(**{marker: "20261108-000000-x/a1"}))
    assert reports(client) == []
    assert hooks.desktop_report("UserPromptSubmit", payload("UserPromptSubmit"),
                                desktop_env(**{marker: "x"})) is None


@LAWS
@given(entry=st.one_of(st.none(), st.text(max_size=20)),
       markers=st.dictionaries(st.sampled_from(["SUBFLEET_ATTEMPT", "SUBFLEET_JOB"]),
                               st.text(min_size=1, max_size=8).filter(lambda value: value.strip()), max_size=2),
       event=st.sampled_from(["SessionStart", "UserPromptSubmit", "PostToolUse", "PreToolUse"]))
def test_c10_9_invariant_4_only_a_desktop_session_outside_a_launch_reports(entry, markers, event):
    """Invariant 4, first half: any launch marker, any entry point but
    `claude-desktop`, or any other event, and nothing is reported."""
    env = desktop_env(**markers)
    if entry is None:
        env.pop("CLAUDE_CODE_ENTRYPOINT")
    else:
        env["CLAUDE_CODE_ENTRYPOINT"] = entry
    found = hooks.desktop_report(event, payload(event), env)
    should = (not markers and entry == "claude-desktop" and event in ("SessionStart", "UserPromptSubmit"))
    assert (found is not None) is should


NAME = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_", min_size=1, max_size=16)


@LAWS
@given(extra=st.dictionaries(st.one_of(NAME.map(lambda name: "CLAUDE_CODE_" + name), NAME),
                             st.text(max_size=30), max_size=8),
       email=st.sampled_from([EMAIL, "Other.Person@Example.com"]),
       account=st.sampled_from([ACCOUNT, ACCOUNT.upper()]), org=st.sampled_from([ORG, ORG.upper()]))
def test_c10_9_invariant_4_the_report_carries_exactly_three_values_and_the_session(extra, email, account, org):
    """Invariant 4, second half (mutant 2's test): whatever other names the
    environment holds, the report is the three values, the session and the time."""
    names = {"CLAUDE_CODE_USER_EMAIL": email, "CLAUDE_CODE_ACCOUNT_UUID": account,
             "CLAUDE_CODE_ORGANIZATION_UUID": org}
    env = {**{key: value for key, value in extra.items()
              if key not in (*names, "CLAUDE_CODE_ENTRYPOINT", "SUBFLEET_ATTEMPT", "SUBFLEET_JOB")},
           "CLAUDE_CODE_ENTRYPOINT": "claude-desktop", **names}
    report = hooks.desktop_report("UserPromptSubmit", payload("UserPromptSubmit"), env)
    assert set(report) == REPORT_KEYS
    assert {key: report[key] for key in REPORT_KEYS - {"observed_at"}} == {
        "email": email, "account_uuid": account.lower(), "org_uuid": org.lower(), "session_id": SESSION}
    assert desktop_app.account_report(**{key: report[key] for key in ("email", "account_uuid", "org_uuid",
                                                                       "session_id", "observed_at")}) == report


class ReadOnly(dict):
    """An environment that records every name read from it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read = []

    def get(self, key, default=None):
        self.read.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.append(key)
        return super().__getitem__(key)


def test_c10_9_the_report_reads_only_its_names(tmp_path):
    env = ReadOnly(desktop_env(CLAUDE_CODE_OAUTH_TOKEN="dummy", HOME="/nowhere", CLAUDE_CODE_SESSION_ID="other"))
    assert hooks.desktop_report("SessionStart", payload("SessionStart"), env) is not None
    assert set(env.read) <= {"SUBFLEET_ATTEMPT", "SUBFLEET_JOB", "CLAUDE_CODE_ENTRYPOINT",
                             *desktop_app.ACCOUNT_NAMES}


@pytest.mark.parametrize("broken", [
    {"CLAUDE_CODE_USER_EMAIL": None}, {"CLAUDE_CODE_USER_EMAIL": "two@@example.com"},
    {"CLAUDE_CODE_USER_EMAIL": "x" * 250 + "@example.com"}, {"CLAUDE_CODE_ACCOUNT_UUID": "not-a-uuid"},
    {"CLAUDE_CODE_ORGANIZATION_UUID": ""}, {"CLAUDE_CODE_ORGANIZATION_UUID": None},
])
def test_c10_9_a_missing_or_malformed_value_sends_nothing(broken, tmp_path):
    env = {key: value for key, value in desktop_env(**broken).items() if value is not None}
    client = FakeClient({"notice.pending": {"notices": []}}, tmp_path)
    hooks.session_event("UserPromptSubmit", payload("UserPromptSubmit"), tmp_path, client=client,
                        stdout=io.StringIO(), env=env)
    assert reports(client) == []


def test_c10_9_a_payload_without_a_session_sends_nothing(tmp_path):
    assert hooks.desktop_report("SessionStart", {"hook_event_name": "SessionStart"}, desktop_env()) is None


# --- invariant 5: the hook is otherwise unchanged ------------------------------------

FAILURES = [DaemonError(2, "refused"), DaemonError(69, "busy"), DaemonUnavailable("no socket"),
            RequestTimeout(), ResponseLost("lost", op=desktop_app.REPORT_OP, request_id=""),
            ProtocolError("malformed"), OSError("reset"), ValueError("junk"), RuntimeError("anything")]


def notice(notice_id: int, job_id: str | None = "20261108-000000-demo", text: str = "run finished") -> dict:
    return {"notice_id": notice_id, "session_id": SESSION, "job_id": job_id, "state": "pending", "text": text}


PENDING = st.one_of(
    st.lists(st.builds(notice, st.integers(1, 9), st.sampled_from(["20261108-000000-demo", None]),
                       st.sampled_from(["run finished", "a ping"])), max_size=3).map(lambda rows: {"notices": rows}),
    st.sampled_from([DaemonUnavailable("down"), DaemonError(69, "busy"), ProtocolError("bad"), OSError("x")]))
REPLIES = st.one_of(st.sampled_from(FAILURES), st.sampled_from([{"recorded": True}, {"recorded": False}, {}, None]))


@LAWS
@given(event=st.sampled_from(["SessionStart", "UserPromptSubmit"]), pending=PENDING, reply=REPLIES,
       env=st.sampled_from([desktop_env(), desktop_env(CLAUDE_CODE_USER_EMAIL="broken"),
                            desktop_env(SUBFLEET_TURN_JOB="20261108-000000-turn"),
                            desktop_env(SUBFLEET_ATTEMPT="20261108-000000-turn/a1"),
                            {"CLAUDE_CODE_ENTRYPOINT": "cli"}, {}]),
       session=st.sampled_from([SESSION, None]), source=st.sampled_from(["startup", "resume", None]))
def test_c10_9_invariant_5_stdout_stderr_and_exit_equal_the_base_hooks(event, pending, reply, env, session, source,
                                                                       tmp_path_factory):
    """Invariant 5 (and mutant 8's test): for generated payloads and daemon
    replies, including a refused, timed-out, unreachable or junk report, the
    hook's stdout, stderr, exit code and notice calls equal release/217's."""
    root = tmp_path_factory.mktemp("hook")
    body = payload(event, **({"source": source} if source else {}))
    if session is None:
        body.pop("session_id")
    base, new = load_base_hooks(), hooks
    base_client = FakeClient({"notice.pending": pending, "notice.mark": {"notices": []}}, root)
    new_client = FakeClient({"notice.pending": pending, "notice.mark": {"notices": []},
                             desktop_app.REPORT_OP: reply}, root)
    expected = run_event(base, event, body, root, base_client, env)
    found = run_event(new, event, body, root, new_client, env)
    assert found == expected
    assert [call for call in new_client.calls if call[0] != desktop_app.REPORT_OP] == base_client.calls


@pytest.mark.parametrize("failure", FAILURES, ids=lambda exc: type(exc).__name__)
@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_c10_9_a_failed_report_never_changes_the_exit_code(failure, event, tmp_path, monkeypatch):
    """Mutant 8's test, through `subfleet hook`: exit 0 and the same words, never 2."""
    for name, value in desktop_env().items():
        monkeypatch.setenv(name, value)
    for name in ("SUBFLEET_ATTEMPT", "SUBFLEET_JOB", "SUBFLEET_TURN_JOB"):
        monkeypatch.delenv(name, raising=False)
    outputs = []
    for reply in (failure, {"recorded": True}):
        client = FakeClient({"notice.pending": {"notices": [notice(4)]}, "notice.mark": {"notices": []},
                             desktop_app.REPORT_OP: reply}, tmp_path)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = hooks.run(event, stream=io.StringIO(json.dumps(payload(event))), root=tmp_path, client=client)
        assert reports(client), "the desktop environment reported"
        outputs.append((code, stdout.getvalue(), stderr.getvalue()))
    assert outputs[0] == outputs[1]
    assert outputs[0][0] == 0 and "run finished" in outputs[0][1] and outputs[0][2] == ""


def test_c10_9_a_report_that_cannot_reach_a_daemon_returns_within_its_second(tmp_path):
    """No daemon at all: the real client's refused connect drops the report at once."""
    import time
    started = time.monotonic()
    hooks.send_desktop_report(tmp_path, hooks.desktop_report("SessionStart", payload("SessionStart"), desktop_env()))
    assert time.monotonic() - started < desktop_app.REPORT_TIMEOUT_S + 0.5
