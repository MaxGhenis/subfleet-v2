"""Round-2 regressions for #179; every provider call uses a fake opener."""
from __future__ import annotations

import io
import json
import threading
import urllib.error
from datetime import timedelta

import pytest

from subfleet.adapters.codex import CodexAdapter, WHAM_RESET_CREDITS_CONSUME_URL
from subfleet.actions import _bounded
from subfleet.store import Store
from tests.unit.test_reset_credit_properties import fake_transport_clock  # noqa: F401 (fixture)
from tests.unit.test_reset_credit_review_r1 import GIFTS, Wham, _resets, posts, view_at
from tests.unit.test_timers_reset_credits import NOW, lane as limited_lane, wants


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_with_reset_body_blocks_another_consume(tmp_path, status):
    with Store(tmp_path / "state.db") as store:
        limited_lane(store, tmp_path, 1)
        http = Wham(GIFTS, (status, b'{"code":"reset","windows_reset":2}'))
        resets = _resets(store, http)
        first = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store))
        later = NOW + timedelta(seconds=60)
        second = resets.evaluate(view_at(store, later), now=later, demand=wants(store))
        assert (first["status"], second["status"], len(posts(http))) == ("unknown", "unsettled-action", 1)
        recorded = json.loads(store.get_action(first["action_id"])["result_json"])
        assert recorded["http_status"] == status
        assert store.list_closures(active_at=later.isoformat())


# C-23.16 (d)'s exhaustive outcome categories. Unexpected codes fail closed.
@pytest.mark.parametrize("status,body,state", [
    (200, b'{"code":"reset","windows_reset":2}', "confirmed"),
    (299, b'{"code":"reset","windows_reset":true}', "unknown"),
    (204, b"", "unknown"),
    (300, b'{"error":{"code":"already_redeemed"}}', "unknown"),
    (399, b"{}", "unknown"),
    (409, b'{"error":{"code":"already_redeemed"}}', "failed"),
    (409, b'{"error":{"code":"already_consumed"}}', "failed"),
    (429, b'{"error":{"code":"rate_limited"}}', "failed"),
    (403, b"{}", "unknown"),
    (408, b'{"error":{"code":"rate_limited"}}', "unknown"),
    (409, b'{"code":"reset","windows_reset":2}', "unknown"),
    (429, b'{"error":{"code":"rate_limited"},"code":"reset","windows_reset":2}', "unknown"),
    (409, b'{"error":{"code":"already_redeemed"},"result":{"code":"reset","windows_reset":2}}', "unknown"),
    (409, b'{"error":{"code":"already_redeemed","windows_reset":2}}', "unknown"),
    (429, b'{"error":{"code":"rate_limited","message":"rejected"}}', "failed"),
    (499, b"<html>", "unknown"),
    (500, b'{"code":"reset","windows_reset":2}', "unknown"),
    (599, b'{"error":{"code":"already_redeemed"}}', "unknown"),
    (199, b"{}", "unknown"),
    (600, b"{}", "unknown"),
])
def test_http_outcome_table_blocks_unless_definitely_unspent(tmp_path, status, body, state):
    with Store(tmp_path / "state.db") as store:
        limited_lane(store, tmp_path, 1)
        http = Wham(GIFTS, (status, body))
        resets = _resets(store, http)
        first = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store))
        later = NOW + timedelta(seconds=60)
        second = resets.evaluate(view_at(store, later), now=later, demand=wants(store))
        assert first["status"] == store.get_action(first["action_id"])["state"] == state
        assert second["status"] == {"unknown": "unsettled-action", "confirmed": "interval-blocked",
                                    "failed": "failed"}[state]
        assert len(posts(http)) == (2 if state == "failed" else 1)


@pytest.mark.parametrize("body,refused_statuses", [
    ({"code": "reset", "windows_reset": 2}, ()),
    ({"error": {"code": "already_redeemed"}}, (409,)),
    ({"error": {"code": "already_consumed"}}, (409,)),
    ({"error": {"code": "rate_limited"}}, (429,)),
    ({"error": {"code": "rate_limited"}, "windows_reset": 2}, ()),
    ({}, ()),
    ({"error": {"code": "already_redeemed"}, "result": {"code": "reset", "windows_reset": 2}}, ()),
    ({"error": {"code": "already_redeemed", "windows_reset": 2}}, ()),
    ({"error": {"code": "rate_limited", "message": {"code": "reset"}}}, ()),
    ({"error": {"code": "rate_limited", "message": "rejected"}}, (429,)),
])
@pytest.mark.parametrize("raised", [False, True], ids=["opener-return", "http-error"])
def test_consume_adapter_classifies_every_http_status(body, refused_statuses, raised):
    """Both transport paths, every integer status 100–699, including unknown codes."""
    raw = {"tokens": {"access_token": "test-only", "account_id": "1"}}
    encoded = json.dumps(body).encode()
    for status in range(100, 700):
        def opener(request, timeout):
            assert request.get_method() == "POST"
            if raised:
                raise urllib.error.HTTPError(request.full_url, status, "fixture", None, io.BytesIO(encoded))
            return status, encoded

        answer = CodexAdapter(opener=opener)._request(raw, WHAM_RESET_CREDITS_CONSUME_URL, payload={})
        expected = "ok" if 200 <= status < 300 else "refused" if status in refused_statuses else "unknown"
        assert answer["status"] == expected, (status, body, answer)


@pytest.mark.parametrize("reason", ["deadline", "cancel", "occupied-slot"])
def test_aborted_before_submission_has_no_provider_call(reason):
    calls, cancel, guard = [], threading.Event(), threading.Lock()
    if reason == "cancel":
        cancel.set()
    if reason == "occupied-slot":
        guard.acquire()
    try:
        result = _bounded(lambda: calls.append("submitted"), cancel,
                          -1 if reason == "deadline" else 60, guard)
        assert result["status"] == "aborted-before-submission"
        assert calls == []
    finally:
        if guard.locked():
            guard.release()


@pytest.mark.parametrize("error", [TimeoutError("timeout"), ConnectionResetError("dropped"), ValueError("unexpected")])
def test_exception_after_submission_is_unknown_and_blocks(tmp_path, error):
    with Store(tmp_path / "state.db") as store:
        limited_lane(store, tmp_path, 1)
        http = Wham(GIFTS, error)
        resets = _resets(store, http)
        first = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store))
        later = NOW + timedelta(seconds=60)
        second = resets.evaluate(view_at(store, later), now=later, demand=wants(store))
        assert (first["status"], second["status"], len(posts(http))) == ("unknown", "unsettled-action", 1)


def test_missing_credential_at_consume_proves_no_submission(tmp_path, monkeypatch):
    with Store(tmp_path / "state.db") as store:
        target = limited_lane(store, tmp_path, 1)
        auth = tmp_path / "codex-1" / "auth.json"
        credential = auth.read_text()
        http = Wham(GIFTS, (200, b'{"code":"reset","windows_reset":2}'))
        resets = _resets(store, http)
        claim = resets.actions.claim

        def claim_then_remove_credential(*args, **kwargs):
            claimed = claim(*args, **kwargs)
            auth.unlink()
            return claimed

        monkeypatch.setattr(resets.actions, "claim", claim_then_remove_credential)
        first = resets.evaluate(view_at(store, NOW), now=NOW, demand=wants(store))
        assert first["status"] == "failed" and posts(http) == []
        assert json.loads(store.get_action(first["action_id"])["result_json"])["status"] == "no-auth"
        assert target.lane_id == "codex-1"
        auth.write_text(credential)
        monkeypatch.setattr(resets.actions, "claim", claim)
        later = NOW + timedelta(seconds=60)
        second = resets.evaluate(view_at(store, later), now=later, demand=wants(store))
        assert second["status"] == "confirmed" and len(posts(http)) == 1
