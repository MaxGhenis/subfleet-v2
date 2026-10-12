"""Exception secrecy at suggestion wire, retry, control-loop and CLI boundaries."""

from __future__ import annotations

import io
import json
import sqlite3
import threading
from contextlib import redirect_stderr

import pytest
from hypothesis import given, settings
import hypothesis.strategies as st

from subfleet import cli, protocol
from subfleet.adapters.base import AdapterError
from subfleet.client import DaemonError, DaemonUnavailable
from subfleet.conversations import suggestions
from subfleet.conversations.store import ConversationError
from tests.unit.test_suggestion_secret_properties import (
    PERSON, assert_secret_absent, fixture, padding, said, secrets, separators,
)


def reply(service, op="suggestion.start", args=None):
    class Connection:
        sent = b""

        def sendall(self, data):
            self.sent += data

    connection = Connection()
    service.respond(connection, threading.Lock(), protocol.Request(op, args or {}), PERSON)
    return json.loads(connection.sent)


ERROR_TYPES = (ConversationError, protocol.ProtocolError, AdapterError,
               ValueError, TypeError, KeyError, OSError, sqlite3.OperationalError)
SUGGESTION_OPS = ("suggestion.create", "suggestion.list", "suggestion.start",
                  "suggestion.dismiss", "suggestion.withdraw")


@given(before=padding, left=separators, secret=secrets, right=separators, after=padding,
       error_type=st.sampled_from(ERROR_TYPES), op=st.sampled_from(SUGGESTION_OPS))
@settings(max_examples=160, deadline=None)
def test_all_suggestion_responder_errors_and_notice_fields_are_scrubbed(
        before, left, secret, right, after, error_type, op):
    text = said(before, left, secret, right, after)
    if error_type is ConversationError:
        error = error_type(text, text, code=7, fix=text)
    elif error_type in (protocol.ProtocolError, AdapterError):
        error = error_type(text, fix=text)
    else:
        error = error_type(text)
    with fixture() as service:
        def fail(*args, **kwargs):
            raise error

        service.handle = fail
        response = reply(service, op)
        assert response["ok"] is False
        refusal = response["error"]
        # UIModel.describe displays these two real wire fields as a notice.
        notice = refusal["message"] + (" (" + refusal["fix"] + ")" if refusal.get("fix") else "")
        assert_secret_absent({"response": response, "log": service.captured.lines, "notice": notice}, secret)
        for value in (refusal["message"], refusal.get("fix"), *service.captured.lines):
            if value is not None:
                assert len(value) <= suggestions.ERROR_MAX and "\n" not in value
        assert len(notice) <= 2 * suggestions.ERROR_MAX + 3
        assert refusal["code"] == (7 if error_type in (ConversationError, AdapterError) else
                                    1 if error_type in (OSError, sqlite3.OperationalError) else 2)


@given(secret=secrets, before=padding, closing=st.booleans())
@settings(max_examples=60, deadline=None)
def test_resumption_errors_are_scrubbed_by_the_outer_control_loop(secret, before, closing):
    with fixture() as service:
        # Exercise tick's real catch without starting its unrelated workers.
        for name in ("_lift_stale_fences", "_catalog_tick", "_moot_blocks", "_replay_final_wakes",
                     "_dispatch", "_adopt_runners", "_replay_unsettled", "_settle_unstarted",
                     "_close_ended_windows", "_reap_runners", "_compact"):
            setattr(service, name, lambda: None)
        service.wakes.control_tick = lambda: None

        def fail():
            service._closed = closing
            raise sqlite3.OperationalError(before + " " + secret)

        service._resume_suggestions = fail
        try:
            service.tick()
        finally:
            service._closed = False
        assert service.captured.lines
        assert_secret_absent(service.captured.lines, secret)
        assert all(len(line) <= suggestions.ERROR_MAX for line in service.captured.lines)


@given(secret=secrets, before=padding, where=st.sampled_from(("brief", "folder", "daemon", "unavailable", "protocol")))
@settings(max_examples=80, deadline=None)
def test_suggest_cli_notices_scrub_local_and_daemon_errors(secret, before, where):
    text = before + " " + secret
    with fixture() as service, pytest.MonkeyPatch.context() as patch:
        patch.setenv("SUBFLEET_HOME", str(service.root))
        patch.setattr(cli, "session_id", lambda: None)
        args = cli.build_parser().parse_args(["suggest", "--list"] if where not in ("brief", "folder") else
                                            ["suggest", "--title", "Fixture", "--tldr", "Fixture",
                                             "--brief-file", "fixture.md"])
        if where == "brief":
            error = OSError(text)
        elif where == "folder":
            error = OSError(text)
            patch.setattr(cli, "open", lambda *a, **kw: io.StringIO("fixture"), raising=False)
        elif where == "daemon":
            error = DaemonError(7, text, text)
        elif where == "unavailable":
            error = DaemonUnavailable(text, fix=text)
        else:
            error = protocol.ProtocolError(text, fix=text)

        def fail(*args, **kwargs):
            raise error

        if where == "brief":
            patch.setattr(cli, "open", fail, raising=False)
        elif where == "folder":
            patch.setattr(cli.Path, "resolve", fail)
        else:
            patch.setattr(cli.Client, "call", fail)
        output = io.StringIO()
        with redirect_stderr(output):
            code = cli.cmd_suggest(args)
        assert code in (1, 2, 7, 69)
        assert_secret_absent(output.getvalue(), secret)
        assert all(len(line) <= suggestions.ERROR_MAX + 20 for line in output.getvalue().splitlines())


@pytest.mark.parametrize("key", ["password", "token"])
@pytest.mark.parametrize("line", ["\nauthorization: abcdefgh", "\ncookie: abcdefgh"])
def test_a_header_inside_a_quoted_value_cannot_destroy_its_closing_quote(key, line):
    secret = key + '="SECRET7-left' + line + '-右秘密-TAIL8"'
    assert_secret_absent(suggestions.scrub_error(secret), secret)
    words, _ = suggestions.normalize("Fixture", "Fixture", secret)
    assert_secret_absent(words, secret)


def test_a_system_reminder_cannot_destroy_a_sensitive_values_closing_quote():
    secret = 'password="SECRET7-left<system-reminder>右秘密-TAIL8"'
    assert_secret_absent(suggestions.scrub_error(secret + '</system-reminder>'), secret)


def test_a_brief_is_scrubbed_again_after_line_end_normalization():
    secret = "authorization: Basic SECRET7-left-右秘密-TAIL8"
    words, _ = suggestions.normalize("Fixture", "Fixture", "ordinary\r" + secret)
    assert_secret_absent(words, secret)
