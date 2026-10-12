"""C-25.7: retained events and real request envelopes exclude diagnostics' inputs."""

from __future__ import annotations

import json
import logging
import threading
import uuid

import pytest

from subfleet import protocol
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
        line = protocol.encode({"v": v, "op": op, "id": id, "args": args or {}})
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
        assert not response.get("id")
