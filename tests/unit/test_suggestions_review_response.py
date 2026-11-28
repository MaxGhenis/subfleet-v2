"""Exercise suggestion.start through the real response and logging boundary."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid

import pytest

from subfleet import protocol
from tests.unit.test_suggestion_secret_properties import AGENT, PERSON, fixture, origin_turn


@pytest.mark.parametrize("error_type", [ValueError, OSError, sqlite3.OperationalError])
def test_r2b_any_start_error_is_scrubbed_at_the_wire_and_log_boundary(error_type, monkeypatch):
    secret = "sk-ant-api03-" + "A" * 40
    with fixture() as service:
        _, job = origin_turn(service)
        row = service.handle("suggestion.create", {
            "request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture", "brief": "Fixture brief",
            "workspace": str(service.folder / "work"), "calling_job": job,
        }, AGENT)["suggestion"]

        def fail(**kwargs):
            raise error_type("failure said " + secret)
        monkeypatch.setattr(service.store, "submit_message", fail)

        class Connection:
            sent = b""

            def sendall(self, data):
                self.sent += data

        connection = Connection()
        service.respond(connection, threading.Lock(),
                        protocol.Request("suggestion.start", {"suggestion_id": row["suggestion_id"]}), PERSON)
        response = json.loads(connection.sent)
        assert response["ok"] is False
        shown = {"wire": response, "log": service.captured.lines}
        assert secret not in json.dumps(shown), shown
