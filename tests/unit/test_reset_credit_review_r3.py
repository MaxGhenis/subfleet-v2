"""Executed #179 round-three probes. All transport and stores are task-local fakes."""
from __future__ import annotations

import http.client
import io
import json
import urllib.request
from datetime import timedelta

import pytest

from subfleet.actions import ResetCredits
from subfleet.store import Store
from tests.unit.test_reset_credit_properties import Step, World, fake_transport_clock, play  # noqa: F401
from tests.unit.test_reset_credit_review_r1 import GIFTS, Wham, _resets, posts, view_at
from tests.unit.test_timers_reset_credits import NOW, lane as limited_lane, wants


EDGE_BODIES = [
    pytest.param(200, b'{"code":"reset","windows_reset":true}', "unknown", id="bool"),
    pytest.param(200, b'{"code":"reset","windows_reset":0}', "unknown", id="zero"),
    pytest.param(200, b'{"code":"reset","windows_reset":-1}', "unknown", id="negative"),
    pytest.param(200, b'{"code":"reset","windows_reset":"2"}', "unknown", id="string"),
    pytest.param(200, b'{"code":"reset","windows_reset":2.0}', "unknown", id="float"),
    pytest.param(200, b'{"code":"reset"}', "unknown", id="missing"),
    pytest.param(409, b'{"error":{"code":"already_redeemed"},"extra":1}', "unknown", id="409-extra"),
    pytest.param(429, b'{"error":{"code":"rate_limited","message":42}}', "unknown", id="429-message-number"),
    pytest.param(303, b'{"code":"reset","windows_reset":2}', "unknown", id="303"),
    pytest.param(307, b'{"code":"reset","windows_reset":2}', "unknown", id="307"),
    pytest.param(200, b'not JSON', "unknown", id="non-json-200"),
    pytest.param(409, b'not JSON', "unknown", id="non-json-409"),
    pytest.param(200, b'{"code":"reset","windows_reset":2}', "confirmed", id="confirmed-control"),
    pytest.param(409, b'{"error":{"code":"already_consumed","message":"refused"}}', "failed", id="refused-control"),
]


@pytest.mark.parametrize("status,body,expected", EDGE_BODIES)
def test_answer_edges_block_another_post(tmp_path, status, body, expected):
    with Store(tmp_path / "state.db") as store:
        limited_lane(store, tmp_path, 1)
        http = Wham(GIFTS, (status, body))
        resets = _resets(store, http)
        first = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store), clock=lambda: NOW)
        later = NOW + timedelta(seconds=60)
        second = resets.evaluate(view_at(store, later), now=later, demand=wants(store), clock=lambda: later)
        observation = (first["status"], second["status"], len(posts(http)))
        print("EDGE", status, body.decode(), observation)
        assert observation == (expected, {"unknown": "unsettled-action", "confirmed": "interval-blocked",
                                         "failed": "failed"}[expected], 2 if expected == "failed" else 1)
        assert store.get_action(first["action_id"])["state"] == expected
        if expected == "unknown":
            assert store.list_closures(active_at=later.isoformat())


class Wire:
    """Feed real urllib/http.client handlers without opening a network socket.

    A fake socket captures HTTP bytes passed to sendall. Listing returns two
    gifts. The consume either raises at connect/body-write, or redirects to a
    GET that responds with an explicit refusal. urllib's production redirect
    and error handlers remain in charge.
    """

    def __init__(self, *, redirect=302, refusal=409, exception=None):
        self.redirect, self.refusal, self.exception = redirect, refusal, exception
        self.requests, self.bodies = [], []

    def install(self, monkeypatch):
        wire = self

        class Connection(http.client.HTTPConnection):
            default_port = 443

            def __init__(self, host, *, context=None, **kwargs):
                # HTTPSConnection's explicit super(HTTPSConnection, self) uses
                # the module's patched class name and would recurse. Keep the
                # real HTTP machinery, replacing just its socket transport.
                super().__init__(host, **kwargs)

            def connect(self):
                if self._method == "POST" and wire.exception == "before-write":
                    raise ConnectionRefusedError("fake connect: no socket write")
                self.sock = Socket()

        class Socket:
            def sendall(self, data):
                data = bytes(data)
                if data.startswith((b"GET ", b"POST ")):
                    self.method, self.path, _ = data.split(b"\r\n", 1)[0].decode().split(" ")
                    wire.requests.append((self.method, self.path))
                else:
                    wire.bodies.append(data)
                    if wire.exception == "after-write":
                        raise ConnectionResetError("fake disconnect after body write")

            def makefile(self, mode):
                if self.method == "GET" and "?redirected" not in self.path:
                    status, body, extra = 200, json.dumps({"credits": GIFTS}).encode(), b""
                elif self.method == "POST":
                    status, body = wire.redirect, b'{"code":"reset","windows_reset":2}'
                    extra = (b"Location: https://chatgpt.com/backend-api/wham/"
                             b"rate-limit-reset-credits/consume?redirected=1\r\n")
                else:
                    status = wire.refusal
                    code = "already_redeemed" if status == 409 else "rate_limited"
                    body, extra = json.dumps({"error": {"code": code}}).encode(), b""
                return io.BytesIO(f"HTTP/1.1 {status} fixture\r\nContent-Length: {len(body)}\r\n".encode()
                                  + extra + b"Connection: close\r\n\r\n" + body)

            def close(self):
                pass

        monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
        # Every opener uses real handlers with fake sockets and no proxies.
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
        monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener(urllib.request.ProxyHandler({})))


def _default_transport_resets(store):
    return ResetCredits(store, {"reset_credits": {"enabled": True, "min_interval_min": 30}})


@pytest.mark.parametrize("exception", ["before-write", "after-write"])
def test_exceptions_at_socket_boundary_are_conservatively_unknown(tmp_path, monkeypatch, exception):
    wire = Wire(exception=exception)
    wire.install(monkeypatch)
    with Store(tmp_path / "state.db") as store:
        limited_lane(store, tmp_path, 1)
        resets = _default_transport_resets(store)
        first = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store), clock=lambda: NOW)
        later = NOW + timedelta(seconds=60)
        second = resets.evaluate(view_at(store, later), now=later, demand=wants(store), clock=lambda: later)
        print("SOCKET-SETUP", exception, first, second, wire.requests, wire.bodies)
        recorded = json.loads(store.get_action(first["action_id"])["result_json"])
        print("SOCKET", exception, first["status"], second["status"], wire.requests, wire.bodies, recorded)
        assert (first["status"], second["status"]) == ("unknown", "unsettled-action")
        assert len(wire.bodies) == (0 if exception == "before-write" else 1)
        assert sum(method == "POST" for method, _ in wire.requests) == (0 if exception == "before-write" else 1)


@pytest.mark.parametrize("redirect", [302, 303, 307])
@pytest.mark.parametrize("refusal", [409, 429])
def test_default_transport_redirect_cannot_turn_a_spend_into_failed(tmp_path, monkeypatch, redirect, refusal):
    wire = Wire(redirect=redirect, refusal=refusal)
    wire.install(monkeypatch)
    with Store(tmp_path / "state.db") as store:
        limited_lane(store, tmp_path, 1)
        resets = _default_transport_resets(store)
        first = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store), clock=lambda: NOW)
        later = NOW + timedelta(seconds=60)
        second = resets.evaluate(view_at(store, later), now=later, demand=wants(store), clock=lambda: later)
        print("REDIRECT-SETUP", redirect, refusal, first, second, wire.requests, wire.bodies)
        recorded = json.loads(store.get_action(first["action_id"])["result_json"])
        observation = (first["status"], second["status"], sum(method == "POST" for method, _ in wire.requests))
        print("REDIRECT", redirect, refusal, observation, wire.requests, recorded)
        assert observation == ("unknown", "unsettled-action", 1)
        assert recorded["status"] == "unknown"
        assert recorded["http_status"] == redirect
        assert not any("?redirected" in path for _, path in wire.requests)
        assert len(wire.bodies) == 1
        assert store.list_closures(active_at=later.isoformat())


def test_provider_observed_oracle_still_catches_real_interval_violation(monkeypatch):
    original = ResetCredits._gate

    def no_interval(self, history, reconciled, instant, settings):
        return original(self, history, reconciled, instant, {**settings, "min_interval_min": 0})

    monkeypatch.setattr(ResetCredits, "_gate", no_interval)
    world = World(lanes=(("limited", True, 6, False), ("limited", True, 2, False)),
                  jobs=(frozenset(),), steps=(Step(0, "none", None), Step(10, "used-up", None)),
                  enabled=True, interval=30, adversarial=None)
    violations, decisions = play(world)
    print("REAL-SPEND", violations, decisions)
    assert [status for status, _, _ in decisions] == ["confirmed", "confirmed"]
    assert violations == ["step 1 at +10 min: confirmed: spent 0:10:00 after the last"]
