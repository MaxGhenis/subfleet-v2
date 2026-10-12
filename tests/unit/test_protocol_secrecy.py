"""C-25.7: retained events and real request envelopes exclude diagnostics' inputs."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass

import pytest

from subfleet import descriptors, protocol
from subfleet.adapters.base import AdapterError
from subfleet.daemon import Daemon
from subfleet.conversations import suggestions
from tests.unit.test_suggestion_secret_properties import (
    AGENT, PERSON, assert_secret_absent, fixture, origin_turn,
)

VALUE = 'failure said "ok" password=\'SECRET7-left\n右秘密-TAIL8\''


class Connection:
    sent = b""

    def getpeername(self):
        return "fixture-client"

    def sendall(self, data):
        self.sent += data


def wire_reply(service=None, *, op="suggestion.create", args=None, v=1, id="safe-id", line=None,
               error=None):
    """Use the daemon's actual decoder, then its or the conversation responder."""
    daemon = object.__new__(Daemon)
    daemon._end_stream = lambda conn: None
    daemon.log = logging.getLogger("protocol-secrecy")
    conn, lock = Connection(), threading.Lock()
    if line is None:
        line = protocol.encode({"v": v, "op": op, "id": id, "args": {} if args is None else args})
    request = daemon._decode(conn, lock, line)
    if request is not None:
        if service is not None:
            service.respond(conn, lock, request, PERSON)
        else:
            def dispatch(*args, **kwargs):
                if error is not None:
                    raise error
                return {"accepted": True}

            daemon.dispatch = dispatch
            daemon._answer(conn, lock, request, None, lambda: False)
    return json.loads(conn.sent)


def test_retained_suggestion_event_excludes_round3_diagnostic():
    with fixture() as service:
        cid, job = origin_turn(service)
        row = service.handle("suggestion.create", {
            "request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture", "brief": "Fixture",
            "calling_job": job, "workspace": str(service.folder / "work"),
        }, AGENT)["suggestion"]
        sid = row["suggestion_id"]

        def fail(**kwargs):
            raise KeyError({"x": VALUE})

        service.store.submit_message = fail
        response = wire_reply(service, op="suggestion.start", args={"suggestion_id": sid})
        assert response["ok"] is False
        assert service.suggestions.get(sid)["state"] == suggestions.STARTING
        # Exactly the pre-round-4 renderer: dict args are converted before scrubbing.
        legacy = suggestions.scrubbed_line(str({"x": VALUE}))
        assert "SECRET7-left" in legacy and "TAIL8" in legacy
        event = service.store.one("SELECT seq,data_json FROM events WHERE conversation_id=? AND kind='suggestion' "
                                  "ORDER BY seq DESC LIMIT 1", (cid,))
        body = json.loads(event["data_json"])
        body["start_error"] = legacy
        with service.store.transaction() as tx:
            tx.execute("UPDATE events SET data_json=? WHERE seq=?", (json.dumps(body), event["seq"]))
        events = service.handle("conversation.events", {"conversation_id": cid, "after": 0}, AGENT)
        assert_secret_absent(events, VALUE)
        shown = next(e for e in events["events"] if e["seq"] == event["seq"])
        assert shown["data"]["start_error"] == "Exception: the suggestion operation failed"
        assert shown["data"]["suggestion_id"] == sid
        assert events == service.handle("conversation.events", {"conversation_id": cid, "after": 0}, AGENT)


def test_protocol_version_refusal_excludes_round4_input():
    response = wire_reply(v=VALUE)
    assert response["ok"] is False and response["error"]["code"] == 2
    assert_secret_absent(response, VALUE)


def test_correlation_id_refusal_excludes_round4_input():
    with fixture() as service:
        response = wire_reply(service, id=VALUE, args={
            "request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture", "brief": "Fixture",
            "model": "invalid",
        })
        assert response["ok"] is False and response["error"]["code"] == 2
        assert_secret_absent(response, VALUE)
        assert "id" not in response


@pytest.mark.parametrize("op", protocol.OPS)
@pytest.mark.parametrize("field", ("v", "args", "id"))
def test_every_operation_refuses_untrusted_envelope_fields(op, field):
    response = wire_reply(op=op, **{field: VALUE})
    assert response["ok"] is False and response["error"]["code"] == 2
    assert_secret_absent(response, VALUE)
    assert field in response["error"]["message"]


@pytest.mark.parametrize("args", (None, False, 0, "", [], VALUE))
def test_falsey_and_non_object_args_cannot_bypass_validation(args):
    response = wire_reply(args=args, line=protocol.encode({
        "v": 1, "op": "ping", "id": "safe-id", "args": args,
    }))
    assert response["ok"] is False and response["error"]["code"] == 2
    assert response["error"]["message"] == protocol.REFUSAL_MESSAGES["args-object"]
    assert_secret_absent(response, VALUE)


@pytest.mark.parametrize("version", (True, 1.0, None, [], {}, VALUE))
def test_protocol_version_must_be_an_integer(version):
    response = wire_reply(v=version)
    assert response["ok"] is False and response["error"]["code"] == 2
    assert response["error"]["message"] == protocol.REFUSAL_MESSAGES["bad-version"]
    assert_secret_absent(response, VALUE)


@pytest.mark.parametrize("id", ("", 0, 1234, "safe-id_9", "a" * 128, 10 ** 127))
def test_bounded_correlation_tokens_are_preserved_on_success_and_refusal(id):
    response = wire_reply(id=id)
    assert response["ok"] is True
    assert response.get("id", "") == str(id)
    refused = wire_reply(id=id, error=ValueError(VALUE))
    assert refused["ok"] is False
    assert refused.get("id", "") == str(id)
    assert_secret_absent(refused, VALUE)


@pytest.mark.parametrize("id", (VALUE, "a" * 129, "界", None, True, 1.5, -1, 10 ** 128, [], {},
                                "ghp_" + "A" * 36))
def test_invalid_correlation_id_is_omitted_before_routing(id):
    class NeverRoute:
        def respond(self, *args):
            raise AssertionError("invalid id must not reach a handler")

    response = wire_reply(NeverRoute(), id=id)
    assert response["ok"] is False and response["error"]["code"] == 2
    assert "id" not in response
    assert response["error"]["message"] == protocol.REFUSAL_MESSAGES["bad-id"]


class UnrenderableFailure(TypeError):
    def __str__(self):
        raise AssertionError("request exceptions must not be rendered")

    def __repr__(self):
        raise AssertionError("request exceptions must not be rendered")


@pytest.mark.parametrize("error", (
    ValueError(VALUE), TypeError({"x": VALUE}), KeyError((5, VALUE)),
    protocol.ProtocolError(VALUE, fix=VALUE), AdapterError(VALUE, fix=VALUE), UnrenderableFailure(VALUE),
))
@pytest.mark.parametrize("conversation", (False, True))
def test_generic_argument_refusals_exclude_exception_and_repair_text(error, conversation):
    if conversation:
        with fixture() as service:
            def fail(*args, **kwargs):
                raise error
            service.handle = fail
            response = wire_reply(service, op="conversation.create")
    else:
        response = wire_reply(op="submit", error=error)
    assert response["ok"] is False
    assert_secret_absent(response, VALUE)
    assert response["error"]["message"].startswith("args:")
    assert response["error"]["fix"] is None


@pytest.mark.parametrize("line", (
    b'{"v":1,"op":"ping","args":"password=SECRET7-left',
    b'{"v":1,"op":"ping","args":"password=SECRET7-left\xff"}',
    '{"v":1,"op":"ping","args":' + '9' * 5000 + '}',
    '[' * 2000 + '"password=SECRET7-left"' + ']' * 2000,
    '["password=SECRET7-left"]',
    descriptors.OVERSIZED,
))
def test_malformed_lines_have_template_refusals(line):
    response = wire_reply(line=line)
    assert response["ok"] is False
    assert response["error"]["message"] in protocol.REFUSAL_MESSAGES.values()
    assert "SECRET7-left" not in json.dumps(response)
    assert response["id"] == ""


@pytest.mark.parametrize("op", (VALUE, {"x": VALUE}, [VALUE], 5, None, ""))
def test_unknown_op_never_echoes_its_value(op):
    response = wire_reply(op=op)
    assert response["ok"] is False
    assert response["error"]["message"].startswith(protocol.UNKNOWN_OP)
    assert_secret_absent(response, VALUE)


def test_coercion_and_notice_refusals_never_render_values_or_exceptions():
    @dataclass
    class PoisonArgs:
        value: str

        def __post_init__(self):
            raise UnrenderableFailure(VALUE)

    for operation in (lambda: protocol.coerce_args(PoisonArgs, {"value": VALUE}),
                      lambda: protocol.notice_row({"x": VALUE})):
        with pytest.raises(protocol.ProtocolError) as caught:
            operation()
        assert_secret_absent(str(caught.value), VALUE)


@pytest.mark.parametrize("error", (None, "KeyError: the suggestion operation failed", VALUE, {"x": VALUE}))
def test_retained_event_preserves_snapshot_and_only_known_diagnostics(error):
    data = {"suggestion_id": "sg-fixture", "state": "starting", "title": "Snapshot", "brief": "Original brief",
            "start_error": error, "untrusted_diagnostic": VALUE}
    shown = suggestions.event_view(data)
    assert shown == {key: value for key, value in data.items() if key != "untrusted_diagnostic"} | {
        "start_error": suggestions.stored_error(error),
    }
    assert_secret_absent(shown, VALUE)
    assert suggestions.event_view(shown) == shown
    assert data["start_error"] == error


def test_current_unknown_op_template_retains_cli_discovery():
    response = wire_reply(op=VALUE)
    assert suggestions.response_reason(response["error"]["message"]) == "unknown-op"
    assert suggestions.response_reason(response["error"]["message"] + VALUE) is None


def test_response_helpers_do_not_reflect_unvalidated_ids():
    response = json.loads(protocol.encode(protocol.fail(VALUE, 7, VALUE, VALUE)))
    assert response["ok"] is False and response["error"]["code"] == 2
    assert "id" not in response
    assert_secret_absent(response, VALUE)
    with pytest.raises(protocol.ProtocolError):
        protocol.ok(VALUE, {})


def test_uncorrelated_replies_preserve_native_clients_required_id_field():
    unknown = wire_reply(op=VALUE)
    busy = json.loads(descriptors.busy_answer("the daemon is busy"))
    successful = json.loads(protocol.encode(protocol.ok("", {})))
    for response in (unknown, busy, successful):
        assert response["id"] == ""
    assert_secret_absent(unknown, VALUE)


def test_retained_event_read_does_not_rewrite_storage():
    with fixture() as service:
        cid, job = origin_turn(service)
        service.handle("suggestion.create", {
            "request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture", "brief": "Fixture",
            "calling_job": job, "workspace": str(service.folder / "work"),
        }, AGENT)
        event = service.store.one("SELECT seq,data_json FROM events WHERE conversation_id=? AND kind='suggestion'",
                                  (cid,))
        body = json.loads(event["data_json"])
        body["start_error"] = VALUE
        body["untrusted_diagnostic"] = VALUE
        stored = json.dumps(body)
        with service.store.transaction() as tx:
            tx.execute("UPDATE events SET data_json=? WHERE seq=?", (stored, event["seq"]))
        for _ in range(2):
            assert_secret_absent(service.handle("conversation.events", {"conversation_id": cid}, AGENT), VALUE)
            assert service.store.one("SELECT data_json FROM events WHERE seq=?", (event["seq"],))["data_json"] == stored
