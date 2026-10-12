"""Fixed-vocabulary diagnostics across all suggestion channels (C-25.7)."""

from __future__ import annotations

import io
import sqlite3
import uuid
from contextlib import redirect_stderr

import pytest
from hypothesis import HealthCheck, example, given, settings
import hypothesis.strategies as st

from subfleet import cli
from subfleet.client import DaemonError
from subfleet.conversations import suggestions
from subfleet.conversations.store import ConversationError
from tests.unit.test_suggestion_error_boundaries import reply
from tests.unit.test_suggestion_secret_properties import (
    AGENT, assert_secret_absent, everything_shown, fixture, origin_turn, secrets,
)


class ArbitraryFailure(Exception):
    pass


class UnrenderableFailure(Exception):
    def __str__(self):
        raise AssertionError("exception __str__ must never run")

    def __repr__(self):
        raise AssertionError("exception __repr__ must never run")


def exception(kind, shape, text):
    args = ((text,) if shape == "string" else ({"x": text},) if shape == "dict" else
            ((5, text),) if shape == "tuple" else (5, text, {"nested": [text.encode(), text]}))
    if kind in ("os-str", "os-bytes"):
        filename = text.encode() if kind == "os-bytes" else text
        return OSError(5, text, filename, None, filename)
    cls = {"key": KeyError, "value": ValueError, "unicode": UnicodeError,
           "sqlite": sqlite3.Error, "custom": ArbitraryFailure}[kind]
    return cls(*args)


def cli_error(service, refusal):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SUBFLEET_HOME", str(service.root))
        patch.setattr(cli, "session_id", lambda: None)

        def fail(*args, **kwargs):
            raise DaemonError(refusal["code"], refusal["message"], refusal.get("fix"))

        patch.setattr(cli.Client, "call", fail)
        stream = io.StringIO()
        with redirect_stderr(stream):
            code = cli.cmd_suggest(cli.build_parser().parse_args(["suggest", "--list"]))
        assert code == refusal["code"]
        return stream.getvalue()


def notice(refusal):
    return refusal["message"] + (" (" + refusal["fix"] + ")" if refusal.get("fix") else "")


# JSON caller values may be nested, Unicode, multiline, numeric, or null. Every
# generated text leaf includes the secret, so even repr-escaped structures expose
# the same forbidden token prefix if a boundary regresses to Python rendering.
prose = st.text(st.characters(blacklist_categories=("Cs",)), max_size=90)


@given(secret=secrets, before=prose, after=prose,
       kind=st.sampled_from(("key", "value", "os-str", "os-bytes", "unicode", "sqlite", "custom")),
       shape=st.sampled_from(("string", "dict", "tuple", "mixed")),
       field=st.sampled_from(("model", "provider", "worktree", "workspace", "request_id", "title", "tldr",
                              "brief", "session_id", "calling_job", "permission", "suggestion_id", "limit",
                              "closed_limit", "suggestion_ids", "confirm_widen")),
       caller_shape=st.sampled_from(("string", "dict", "list", "null", "number")))
@example(secret="password='SECRET7-left\n右秘密-TAIL8'", before='failure said "ok" ', after="",
         kind="key", shape="dict", field="model", caller_shape="string")
@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_generated_exception_and_caller_text_never_reaches_any_channel(
        secret, before, after, kind, shape, field, caller_shape):
    text = before + "\n" + secret + "\n" + after
    error = exception(kind, shape, text)
    caller = {"string": text, "dict": {"x": text}, "list": [text], "null": None, "number": 5}[caller_shape]
    with fixture() as service:
        cid, job = origin_turn(service)
        payload = {"request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture",
                   "brief": "Fixture brief", "calling_job": job, "workspace": str(service.folder / "work")}
        row = service.handle("suggestion.create", payload, AGENT)["suggestion"]
        sid = row["suggestion_id"]

        def fail(**kwargs):
            raise error

        service.store.submit_message = fail
        response = reply(service, args={"suggestion_id": sid})
        assert response["ok"] is False
        service._resume_start(sid, 1, service.clock())
        shown = everything_shown(service, sid, cid, "")
        shown.update(wire=response, notice=notice(response["error"]), cli=cli_error(service, response["error"]))
        assert_secret_absent(shown, secret)
        assert service.suggestions.get(sid)["state"] == suggestions.STARTING
        assert service.captured.lines and any("finishing the start" in line for line in service.captured.lines)

        if field in ("limit", "closed_limit", "suggestion_ids"):
            op, args = "suggestion.list", {field: caller}
        elif field in ("permission", "suggestion_id", "confirm_widen"):
            op, args = "suggestion.start", {"suggestion_id": sid, field: caller}
        else:
            op, args = "suggestion.create", {**payload, "request_id": str(uuid.uuid4()), field: caller}
        caller_response = reply(service, op, args)
        assert_secret_absent(caller_response, secret)
        # Both error replies and successful admissions must leave all stored and
        # published suggestion fields clean. Successful title/brief text follows
        # the existing content scrubber; diagnostics never use it as a guarantee.
        assert_secret_absent(everything_shown(service, sid, cid, ""), secret)
        if not caller_response["ok"]:
            refusal = caller_response["error"]
            assert_secret_absent({"notice": notice(refusal), "cli": cli_error(service, refusal)}, secret)


@pytest.mark.parametrize("kind", (KeyError, ValueError, OSError, UnicodeError, sqlite3.Error, UnrenderableFailure))
def test_diagnostics_do_not_render_exception_objects(kind):
    class Poison:
        def __str__(self):
            raise AssertionError("argument __str__ must never run")

        def __repr__(self):
            raise AssertionError("argument __repr__ must never run")

    error = kind(Poison())
    with fixture() as service:
        service.handle = lambda *a, **kw: (_ for _ in ()).throw(error)
        response = reply(service)
        assert response["ok"] is False
        assert "Poison" not in response["error"]["message"]
        assert suggestions.scrub_error(error)


@pytest.mark.parametrize("field", ("reason", "message", "fix"))
def test_conversation_error_metadata_is_untrusted(field):
    value = "password='SECRET7-left\n右秘密-TAIL8'"
    parts = {"reason": "worktree-failed", "message": "failure", "fix": "try again", field: value}
    error = ConversationError(**parts, code=1)
    with fixture() as service:
        service.handle = lambda *a, **kw: (_ for _ in ()).throw(error)
        response = reply(service)
        assert_secret_absent(response, value)
        assert_secret_absent(cli_error(service, response["error"]), value)


@pytest.mark.parametrize("flag", ("--provider", "--permission", "--request-id"))
def test_cli_argument_refusals_do_not_echo_caller_values(flag, capsys):
    value = "password='SECRET7-left\n右秘密-TAIL8'"
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(["suggest", flag, value])
    assert caught.value.code == 2
    assert_secret_absent(capsys.readouterr().err, value)


def test_note_error_refuses_already_rendered_text():
    with fixture() as service:
        payload = {"request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture", "brief": "Fixture"}
        sid = service.handle("suggestion.create", payload, AGENT)["suggestion"]["suggestion_id"]
        service.store.submit_message = lambda **kw: (_ for _ in ()).throw(OSError(5, "untrusted"))
        reply(service, args={"suggestion_id": sid})
        value = 'failure said "ok" password=\'SECRET7-left\n右秘密-TAIL8\''
        row = service.suggestions.note_error(sid, value)
        assert row["start_error"] == "Exception: the suggestion operation failed"
        assert_secret_absent(suggestions.view(row), value)


def test_views_do_not_republish_legacy_free_form_errors():
    with fixture() as service:
        row = service.handle("suggestion.create", {
            "request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture", "brief": "Fixture",
        }, AGENT)["suggestion"]
        value = "password='SECRET7-left\n右秘密-TAIL8'"
        row["start_error"] = value
        assert suggestions.view(row)["start_error"] == "Exception: the suggestion operation failed"
        assert_secret_absent(suggestions.view(row), value)


@pytest.mark.parametrize("kind", (KeyError, ValueError, OSError, UnicodeError, sqlite3.Error, UnrenderableFailure))
@pytest.mark.parametrize("where", ("client", "caller"))
def test_unexpected_cli_exceptions_cannot_escape_to_raw_rendering(kind, where):
    value = "password='SECRET7-left\n右秘密-TAIL8'"
    with fixture() as service, pytest.MonkeyPatch.context() as patch:
        patch.setenv("SUBFLEET_HOME", str(service.root))
        error = kind({"x": value})

        def fail(*args, **kwargs):
            raise error

        patch.setattr(cli, "session_id", fail if where == "caller" else lambda: None)
        patch.setattr(cli.Client, "call", fail)
        stream = io.StringIO()
        with redirect_stderr(stream):
            assert cli.cmd_suggest(cli.build_parser().parse_args(["suggest", "--list"])) in (1, 2)
        assert_secret_absent(stream.getvalue(), value)


def test_outer_cli_parser_does_not_echo_unknown_arguments(capsys):
    value = "password='SECRET7-left\n右秘密-TAIL8'"
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(["suggest", "--unknown-option", value])
    assert caught.value.code == 2
    assert_secret_absent(capsys.readouterr().err, value)
