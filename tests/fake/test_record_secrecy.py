"""C-25.7 at the boundaries review r6 of #181 found open: what a caller or its
environment controls never reaches a wire reply or a persisted row.

Ports of the review's probes (`probe_bypasses.py`), each through the real daemon:

1. an argument check's OS error (`PermissionError` naming a filename, 76d3e2c1c)
   read back by `message.status` and `conversation.watch`;
2. a checkout hook's stderr when admission's `git worktree add` fails, read back
   by `notice.pending`, `why`, `runs show`, `list`, `status` and every stored row;
3. a job id whose slug is a credential (`sk-`, `glpat-`, `xoxb-`), which `show`
   echoed because the timestamp's hyphen hid the token from the scrubber.

Then, as Hypothesis properties over the scrubber's credential families (C-23.14)
and arbitrary Unicode padding: for every such secret in a filename, a hook's
output or a job slug, no wire reply and no row of either store holds it. Raw
detail may reach daemon.log only, through the whole-text scrubber; where the
tests read that log they read it for the diagnostic, not as a wire.
"""
from __future__ import annotations

import json
import sqlite3
import string
import threading
import uuid
from unittest.mock import patch

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, example, given, settings

from subfleet import daemon as daemon_module, protocol
from subfleet.adapters.base import AdapterError
from subfleet.daemon import Daemon
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401  (fixture)
from tests.fake.test_workspace_contract import repository
from tests.unit.test_protocol_secrecy import Connection, wire_reply
from tests.unit.test_suggestion_secret_properties import (
    assert_secret_absent, padding, said, secrets, separators,
)

VALUE = "password='SECRET7-left\n右秘密-TAIL8'"
SETTINGS = {"model": "opus", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}
SESSION = "fake-session"                    # Harness.submit_args's caller session


def answer(daemon, op: str, args: dict) -> dict:
    """One request through the daemon's real responder."""
    conn = Connection()
    daemon._answer(conn, threading.Lock(), protocol.Request(op=op, args=args, id="safe-id"), None, lambda: False)
    return json.loads(conn.sent)


def stored(daemon) -> dict:
    """Every row of both stores, as text."""
    found = {}
    for name in ("state.sqlite3", "conversations.sqlite3"):
        path = daemon.root / name
        if not path.exists():
            continue
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                found[f"{name}:{table}"] = [[value.decode("utf-8", "replace") if isinstance(value, bytes) else value
                                             for value in row] for row in db.execute(f'SELECT * FROM "{table}"')]
    return found


def log_text(daemon) -> str:
    daemon._log_handler.flush()
    return (daemon.root / "daemon.log").read_text()


def assert_not_logged(daemon, secret: str) -> None:
    """daemon.log has a diagnostic only through the whole-text scrubber (C-25.7). The
    log as a whole is not held to `assert_secret_absent`'s prefix check: its own
    words (`disk-…`, `task-…`) can begin as a token does."""
    log = log_text(daemon)
    assert secret not in log and " ".join(secret.split()) not in log


# --- 1. an argument check's OS error, through conversation state ------------------

def conversation_message(daemon) -> tuple[object, str, str]:
    service = daemon.conversations
    work = daemon.root / "conversation-work"
    work.mkdir(exist_ok=True)
    cid = service.store.create_conversation(provider="claude", workspace=str(work), workspace_kind="in-place",
                                            settings=SETTINGS, origin="new")[0]["conversation_id"]
    mid = str(uuid.uuid4())
    service.handle("message.submit", {"conversation_id": cid, "message_id": mid, "text": "fixture"}, None)
    return service, cid, mid


def conversation_views(service, cid: str, mid: str) -> dict:
    return {"status": wire_reply(service, op="message.status", args={"message_ids": [mid]}),
            "watch": wire_reply(service, op="conversation.watch", args={"after": 0, "timeout_s": 0}),
            "open": wire_reply(service, op="conversation.open", args={"conversation_id": cid}),
            "list": wire_reply(service, op="conversation.list", args={})}


def test_an_argument_checks_filename_never_reaches_a_messages_state(state_daemon):
    """The review's P1 probe: `PermissionError(13, …, filename)` while submit reads the
    turn's prompt. 76d3e2c1c keeps the words on the exception for direct callers; the
    message's `state_reason` reads the argument template, as before 76d3e2c1c."""
    daemon, harness = state_daemon
    service, cid, mid = conversation_message(daemon)
    filename = str(daemon.root / VALUE)
    with patch.object(daemon_module, "read_regular", side_effect=PermissionError(13, "Permission denied", filename)):
        service._dispatch()
    message = service.store.message(mid)
    assert (message["state"], message["state_reason"]) == (
        "failed", "not-delivered: " + protocol.REFUSAL_MESSAGES["invalid-args"])
    shown = conversation_views(service, cid, mid)
    assert shown["status"]["result"]["messages"][0]["state_reason"] == message["state_reason"]
    assert_secret_absent([shown, stored(daemon)], VALUE)
    # The words went to daemon.log, scrubbed whole: the errno text is there for diagnosis.
    assert f"turn submit of {mid} refused (ProtocolError): [Errno 13] Permission denied" in log_text(daemon)


@pytest.mark.parametrize("raised,state,reason", [
    (lambda text: AdapterError(f"could not inspect the workdir: {text}", code=1, fix=text, reason="inspect-workdir"),
     "queued", "deferred: could not inspect the workdir"),
    (lambda text: AdapterError(f"writable job refused: {text}", code=7, fix=text, reason="writable-branch",
                               details={"branch": text}),
     "failed", "not-delivered: writable job refused on a protected branch"),
    (lambda text: OSError(5, "Input/output error", text), "queued", "deferred: OSError: [Errno 5] Input/output error"),
    (lambda text: KeyError(text), "queued", "deferred: KeyError"),
    (lambda text: protocol.ProtocolError(text, fix=text), "failed", "not-delivered: operation: request was refused"),
])
def test_no_turn_submit_failure_puts_its_words_in_a_messages_state(state_daemon, raised, state, reason):
    """C-25.7: a handler refusal reads its template, an OS error its errno, anything
    else its class; a permanent one fails the message, any other defers it."""
    daemon, harness = state_daemon
    service, cid, mid = conversation_message(daemon)
    with patch.object(daemon, "submit", side_effect=raised(VALUE)):
        service._dispatch()
    message = service.store.message(mid)
    assert (message["state"], message["state_reason"]) == (state, reason)
    assert_secret_absent([conversation_views(service, cid, mid), stored(daemon)], VALUE)


# --- 2. a checkout hook's output, through a job's records ---------------------------

TRANSIENT_LINE = "fatal: Unable to create '/r/.git/index.lock': File exists.\n"


def hooked(daemon, harness, hook: str, *, transient: bool = False, session: str = SESSION) -> str:
    """A writable dispatch whose worktree's `post-checkout` hook says `hook` and fails.
    A job that waits stays its session's live writer (C-6.4), so a later dispatch in
    the same daemon names a session of its own."""
    workdir = harness.workdir
    if not (workdir / ".git").exists():
        repository(daemon, harness)
    said_file = daemon.root / "said.txt"
    said_file.write_text((TRANSIENT_LINE if transient else "") + hook)
    script = workdir / ".git" / "hooks" / "post-checkout"
    script.write_text(f"#!/bin/sh\ncat '{said_file}' >&2\nexit 1\n")
    script.chmod(0o755)
    return daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", caller_session=session))["job_id"]


def job_views(daemon, job_id: str, session: str = SESSION) -> dict:
    return {name: answer(daemon, op, args) for name, (op, args) in {
        "pending": ("notice.pending", {"session_id": session}),
        "notices": ("notice.list", {"session_id": session, "resolved": True}),
        "show": ("show", {"job_id": job_id}),
        "why": ("why", {"job_id": job_id}),
        "list": ("list", {}),
        "status": ("daemon.status", {}),
    }.items()}


@pytest.mark.parametrize("transient", [False, True])
def test_a_checkout_hooks_output_never_reaches_a_jobs_records(state_daemon, transient):
    """The review's second P1 probe: admission's `git worktree add` fails in the hook.
    A permanent failure fails the job with the `workspace-prepare` template; one whose
    words name a held lock waits (C-6.8). Either way the hook's words are in daemon.log
    only, scrubbed, and every reply and stored row says how git ended."""
    daemon, harness = state_daemon
    job_id = hooked(daemon, harness, VALUE, transient=transient)
    daemon._admit()
    job = daemon.store.get_job(job_id)
    shown = job_views(daemon, job_id)
    if transient:
        assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
        record = shown["show"]["result"]["workspace"]
        assert (record["error_type"], record["error"]) == (
            "SalvageError", "could not allocate worktree: git worktree add exited 1; daemon.log has its output")
        assert shown["why"]["result"]["job"]["hold"]["error"] == record["error"]
        assert shown["pending"]["result"]["notices"] == []
    else:
        assert (job["state"], job["rc"]) == ("failed", 7)
        [notice] = shown["pending"]["result"]["notices"]
        assert notice["text"].splitlines()[1:] == [
            "could not allocate the worktree; daemon.log has git's output; "
            "fix: check repository and state-root permissions"]
    assert_secret_absent([shown, stored(daemon)], VALUE)
    log = log_text(daemon)
    assert f"job {job_id}: git worktree add exited 1: " in log and "[REDACTED]" in log


def test_a_handler_refusal_at_launch_keeps_its_template_not_its_words(state_daemon, monkeypatch):
    """C-25.7 at launch: an `AdapterError` the launch meets ends the attempt with the
    refusal's public text as its detail and notice; its words go to daemon.log."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness)

    def refuse(lane):
        raise AdapterError(f"API-key home refused: {VALUE}", fix=VALUE, reason="api-key-home")
    monkeypatch.setattr(daemon, "_validate_home", refuse)
    Daemon._launch(daemon, attempt)             # the fixture stubs the instance's; this launches nothing either
    receipt = json.loads((adir / "exit.json").read_text())
    assert receipt["spawn_error"] == "API-key home refused; fix: log this lane into a subscription account"
    assert_secret_absent([receipt, stored(daemon)], VALUE)
    assert f"job {job_id} refused (AdapterError): API-key home refused" in log_text(daemon)


# --- 3. a job id whose slug is a credential ----------------------------------------

@pytest.mark.parametrize("token", ["sk-" + "a" * 20, "glpat-" + "a" * 20, "xoxb-" + "a" * 20, "sk-proj-" + "a" * 16,
                                   "build-sk-" + "a" * 20, "r6-glpat-" + "a" * 20])
@pytest.mark.parametrize("op", ["show", "why", "kill", "wait"])
def test_a_job_id_whose_slug_is_a_credential_is_refused_without_it(state_daemon, token, op):
    """The review's P2 probe, on every op that looks a job up: refused as `unknown job`,
    the id left out of the reply, the stores and daemon.log."""
    daemon, harness = state_daemon
    job_id = "20261013-000000-" + token
    args = {"job_ids": [job_id], "deadline_s": 0} if op == "wait" else {"job_id": job_id}
    response = answer(daemon, op, args)
    assert (response["ok"], response["error"]["code"], response["error"]["message"]) == (False, 2, "unknown job")
    assert token not in json.dumps([response, stored(daemon)]) and token not in log_text(daemon)


@pytest.mark.parametrize("slug", ["review-r6", "sk-learn", "glpat", "task-" + "a" * 30, "risk-" + "a" * 20,
                                  "pr181-fix9b"])
def test_an_ordinary_job_slug_is_still_named(state_daemon, slug):
    """The check reads the id as C-1.5 reads a request id, whole and from each hyphen:
    what the scrubber does not recognize there (a short `sk-` word, `sk` ending a
    word such as `task` or `risk`) is named."""
    daemon, harness = state_daemon
    job_id = "20261013-000000-" + slug
    response = answer(daemon, "show", {"job_id": job_id})
    assert response["error"]["message"] == f"unknown job {job_id}"


# --- the properties -------------------------------------------------------------------

#: The credential families the scrubber recognizes within a job slug's alphabet.
slug_secrets = st.tuples(st.sampled_from(["sk-", "sk-proj-", "sk-ant-", "glpat-", "xoxb-", "xoxp-", "xoxa-"]),
                         st.text(string.ascii_lowercase + string.digits + "-", min_size=16, max_size=24)
                         ).map("".join).filter(lambda slug: len(slug) <= 40 and not slug.endswith("-"))


@given(slug=slug_secrets, op=st.sampled_from(["show", "why", "kill", "wait"]))
@example(slug="sk-" + "a" * 20, op="show")
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_no_reply_or_row_holds_a_credential_job_slug(state_daemon, slug, op):
    daemon, harness = state_daemon
    job_id = "20261013-000000-" + slug
    response = answer(daemon, op, {"job_ids": [job_id], "deadline_s": 0} if op == "wait" else {"job_id": job_id})
    assert response["error"]["message"] == "unknown job"
    assert_secret_absent([response, stored(daemon)], slug)


@given(before=padding, left=separators, secret=secrets, right=separators, after=padding,
       transient=st.booleans())
@example(before="", left="", secret='password="SECRET7-left\n右秘密-TAIL8"', right="", after="", transient=False)
@settings(max_examples=30, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
def test_no_reply_or_row_holds_what_a_checkout_hook_said(state_daemon, before, left, secret, right, after,
                                                          transient):
    """For every family and placement: a hook's stderr through admission's worktree,
    then every reply about the job and every stored row."""
    daemon, harness = state_daemon
    session = f"hook-{uuid.uuid4().hex[:12]}"       # the fixture's daemon outlives one example
    job_id = hooked(daemon, harness, said(before, left, secret, right, after), transient=transient, session=session)
    daemon._admit()
    assert daemon.store.get_job(job_id)["state"] == ("waiting" if transient else "failed")
    assert_secret_absent([job_views(daemon, job_id, session), stored(daemon)], secret)
    assert_not_logged(daemon, secret)


@given(before=padding, left=separators, secret=secrets, right=separators, after=padding,
       where=st.sampled_from(["argument check", "handler refusal", "os error", "defect"]))
@example(before="", left="", secret='password="SECRET7-left\n右秘密-TAIL8"', right="", after="",
         where="argument check")
@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
def test_no_reply_or_row_holds_what_a_turn_submit_failure_said(state_daemon, before, left, secret, right, after,
                                                                where):
    """For every family and placement: in a filename an argument check met, a handler
    refusal's words and repair, an OS error's filename or a defect's argument."""
    daemon, harness = state_daemon
    text = said(before, left, secret, right, after)
    service, cid, mid = conversation_message(daemon)
    if where == "argument check":
        failure = patch.object(daemon_module, "read_regular",
                               side_effect=PermissionError(13, "Permission denied", text))
    else:
        failure = patch.object(daemon, "submit", side_effect={
            "handler refusal": AdapterError(text, code=7, fix=text, reason="writable-branch", details={"branch": text}),
            "os error": OSError(5, text, text),
            "defect": KeyError(text),
        }[where])
    with failure:
        service._dispatch()
    assert service.store.message(mid)["state"] in ("failed", "queued")
    assert_secret_absent([conversation_views(service, cid, mid), stored(daemon)], secret)
