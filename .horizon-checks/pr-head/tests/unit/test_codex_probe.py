"""Offline usage and enrollment checks for C-1.4, C-9.7 and C-10.2."""

from __future__ import annotations

import base64
import io
import json
import ssl
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.adapters.codex import CodexAdapter, WHAM_USAGE_URL
from subfleet.contracts import Credential, Lane, LaneOwner, ReadingLabel


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
AUTH_CLAIMS = "https://api.openai.com/auth"
# Fixed, synthetic wham response using the v1 endpoint's recorded schema. The
# weekly window intentionally precedes the five-hour window to catch slot bias.
SAVED_WHAM = b'''{
  "email": "probe@example.com", "plan_type": "pro",
  "rate_limit": {
    "allowed": true, "limit_reached": false,
    "primary_window": {
      "used_percent": 62.5, "limit_window_seconds": 604800,
      "reset_at": 1788782400
    },
    "secondary_window": {
      "used_percent": 12.5, "limit_window_seconds": 18000,
      "reset_at": 1788609600
    }
  }
}'''


def _jwt(claims: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"test-only.{encoded}.not-a-signature"


def _auth(*, account_id: str | None = "account-test", plan: str = "pro",
          email: str | None = "probe@example.com") -> dict:
    claims = {"chatgpt_plan_type": plan}
    if account_id is not None:
        claims["chatgpt_account_id"] = account_id
    return {
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"access_token": _jwt({AUTH_CLAIMS: claims}),
                   "id_token": _jwt({"email": email} if email else {})},
    }


def _home(tmp_path: Path, raw: object | None = None) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps(_auth() if raw is None else raw))
    return home


def _lane(home: Path) -> Lane:
    return Lane("codex-4", "codex", "codex:account-test",
                Credential("codex", str(home), "home"), str(home), LaneOwner.V2, False)


def _adapter(payload: bytes | dict = SAVED_WHAM) -> CodexAdapter:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return CodexAdapter(opener=lambda request, timeout: (200, body), now=lambda: NOW)


def _no_http(request, timeout):
    pytest.fail("Refused or unreadable credentials must not trigger an HTTP request")


def test_saved_wham_returns_fractional_provider_readings_by_duration(tmp_path):
    """C-9.1 C-9.7 C-1.7 Reversed wham slots retain their durations, fractions and UTC clocks."""
    readings = _adapter().probe(_lane(_home(tmp_path)), {})
    assert isinstance(readings, tuple)
    assert len(readings) == 2
    windows = {reading.window: reading for reading in readings}
    assert windows["seven_day"].utilization == 0.625
    assert windows["seven_day"].resets_at == "2026-09-07T12:00:00Z"
    assert windows["five_hour"].utilization == 0.125
    assert windows["five_hour"].resets_at == "2026-09-05T12:00:00Z"
    for reading in readings:
        assert reading.lane_id == "codex-4"
        assert reading.scope == "account"
        assert reading.label == ReadingLabel.PROVIDER
        assert reading.source == "wham"
        assert reading.observed_at == "2026-09-05T12:00:00Z"
        assert reading.attempt_id is None


@pytest.mark.parametrize("slot", ["primary_window", "secondary_window"])
def test_weekly_only_payload_has_one_duration_keyed_reading(tmp_path, slot):
    """C-9.7 A weekly-only response never creates an invented five-hour reading."""
    window = json.loads(SAVED_WHAM)["rate_limit"]["primary_window"]
    readings = _adapter({"rate_limit": {slot: window}}).probe(_lane(_home(tmp_path)), {})
    assert len(readings) == 1
    assert readings[0].window == "seven_day"
    assert readings[0].utilization == 0.625


@pytest.mark.parametrize("minutes,expected", [(60, "60"), (360, "360"), (1440, "1440"),
                                              (300, "five_hour"), (10080, "seven_day")])
def test_explicit_window_minutes_preserve_nonstandard_durations(tmp_path, minutes, expected):
    """C-9.7 Only exact 300/10080 durations use named keys; other windows retain minute counts."""
    readings = _adapter({"rate_limit": {"primary_window": {
        "window_minutes": minutes, "used_percent": 0, "reset_at": "2026-09-05T09:00:00-04:00",
    }}}).probe(_lane(_home(tmp_path)), {})
    assert len(readings) == 1
    assert readings[0].window == expected
    assert readings[0].utilization == 0.0
    assert readings[0].resets_at == "2026-09-05T13:00:00Z"


@pytest.mark.parametrize("invalid", [
    {"window_minutes": True}, {"window_minutes": 0}, {"window_minutes": 2.5},
    {"window_minutes": "300"}, {"used_percent": True}, {"used_percent": -1},
    {"used_percent": 101}, {"used_percent": "20"}, {"used_percent": float("nan")},
])
def test_malformed_window_does_not_discard_other_valid_reading(tmp_path, invalid):
    """C-9.1 C-9.7 Malformed provider windows cannot become percentages or suppress valid windows."""
    window = {"window_minutes": 300, "used_percent": 20, "reset_at": 1788609600, **invalid}
    readings = _adapter({"rate_limit": {
        "primary_window": window,
        "secondary_window": {"window_minutes": 10080, "used_percent": 100},
    }}).probe(_lane(_home(tmp_path)), {})
    assert len(readings) == 1
    assert readings[0].window == "seven_day"
    assert readings[0].utilization == 1.0
    assert readings[0].resets_at is None


def test_default_urllib_probe_sends_subscription_headers_without_writing_home(tmp_path, monkeypatch):
    """C-10.1 C-10.2 C-12.1 The standard-library GET uses auth.json and leaves credentials untouched."""
    raw = _auth()
    home = _home(tmp_path, raw)
    before = (home / "auth.json").read_bytes()
    calls = []

    class Response(io.BytesIO):
        status = 200

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        return Response(SAVED_WHAM)

    monkeypatch.setattr("subfleet.adapters.codex.urllib.request.urlopen", urlopen)
    readings = CodexAdapter(now=lambda: NOW, timeout=2.5).probe(_lane(home), {})
    assert len(readings) == 2
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == WHAM_USAGE_URL
    assert request.get_method() == "GET"
    assert request.data is None
    assert timeout == 2.5
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["authorization"] == f"Bearer {raw['tokens']['access_token']}"
    assert headers["chatgpt-account-id"] == "account-test"
    assert headers["accept"] == "application/json"
    assert "subfleet" in headers["user-agent"]
    assert (home / "auth.json").read_bytes() == before
    assert list(home.iterdir()) == [home / "auth.json"]


@pytest.mark.parametrize("error", [
    OSError("Network unreachable"), TimeoutError("Timed out"),
    urllib.error.URLError("Name resolution failed"), ssl.SSLError("TLS handshake failed"),
    urllib.error.HTTPError(WHAM_USAGE_URL, 401, "Unauthorized", {}, io.BytesIO(b"{}")),
])
def test_probe_network_failure_returns_no_replacement_readings(tmp_path, error):
    """C-9.1 C-9.7 Probe failures return an empty tuple so callers retain their previous readings."""
    def failing_opener(request, timeout):
        raise error

    assert CodexAdapter(opener=failing_opener).probe(_lane(_home(tmp_path)), {}) == ()


@pytest.mark.parametrize("status,body", [
    (401, b'{"error":{"code":"token_revoked"}}'), (500, b"{}"),
    (200, b"not JSON"), (200, b"[]"), (200, b"null"), (200, b"\xff"),
    (200, b'{"rate_limit":[]}'), (200, b'{"rate_limit":{"primary_window":null}}'),
])
def test_unusable_probe_payload_returns_empty_tuple(tmp_path, status, body):
    """C-9.1 C-9.7 HTTP and malformed-payload failures must not invent fresh quota readings."""
    adapter = CodexAdapter(opener=lambda request, timeout: (status, body))
    assert adapter.probe(_lane(_home(tmp_path)), {}) == ()


@pytest.mark.parametrize("raw", [{}, [], {"tokens": "invalid"}, {"OPENAI_API_KEY": "test-only-key"}])
def test_probe_without_subscription_token_does_not_call_endpoint(tmp_path, raw):
    """C-10.2 API-key and unreadable homes cannot supply authenticated subscription readings."""
    assert CodexAdapter(opener=_no_http).probe(_lane(_home(tmp_path, raw)), {}) == ()


@pytest.mark.parametrize("fields", [
    {"OPENAI_API_KEY": "test-only-key"}, {"CODEX_API_KEY": "test-only-key"},
    {"auth_mode": "apikey"}, {"auth_mode": "API_KEY"}, {"auth_mode": "api-key"},
])
def test_enroll_refuses_api_key_logins_even_with_oauth_tokens(tmp_path, fields):
    """C-10.2 C-6.5 API-key markers in mixed homes are refused with code 7 and a subscription fix."""
    home = _home(tmp_path, {**_auth(), **fields})
    with pytest.raises(AdapterError) as caught:
        CodexAdapter(opener=_no_http).enroll(Credential("codex", str(home), "home"))
    assert caught.value.code == 7
    assert caught.value.fix
    assert "subscription" in caught.value.fix.lower()


def test_enroll_refuses_free_plan_in_token_before_probe(tmp_path):
    """C-10.2 A free-plan token is refused with code 7 and a fix before calling the endpoint."""
    home = _home(tmp_path, _auth(plan=" FREE "))
    with pytest.raises(AdapterError) as caught:
        CodexAdapter(opener=_no_http).enroll(Credential("codex", str(home), "home"))
    assert caught.value.code == 7
    assert caught.value.fix


def test_enroll_refuses_free_plan_reported_by_usage_endpoint(tmp_path):
    """C-10.2 Server evidence of a free plan prevents enrollment despite older paid-plan claims."""
    home = _home(tmp_path)
    payload = {**json.loads(SAVED_WHAM), "plan_type": "free"}
    with pytest.raises(AdapterError) as caught:
        _adapter(payload).enroll(Credential("codex", str(home), "home"))
    assert caught.value.code == 7
    assert caught.value.fix


@pytest.mark.parametrize("location", ["tokens", "account_id", "chatgpt_account_id", "id_token"])
def test_enroll_prefers_account_id_to_email(tmp_path, location):
    """C-1.4 C-10.2 Enrollment derives the account key from account claims before email fallback."""
    raw = _auth(account_id=None)
    if location == "tokens":
        raw["tokens"]["account_id"] = "account-specific"
    elif location == "id_token":
        # Access claims have a plan but no account id; identity claims still count.
        raw["tokens"]["id_token"] = _jwt({
            "email": "probe@example.com", AUTH_CLAIMS: {"chatgpt_account_id": "account-specific"},
        })
    else:
        raw["tokens"]["access_token"] = _jwt({AUTH_CLAIMS: {
            "chatgpt_plan_type": "pro", location: "account-specific",
        }})
    home = _home(tmp_path, raw)
    info = _adapter().enroll(Credential("codex", str(home), "home"))
    assert info.account_key == "codex:account-specific"
    assert info.plan == "pro"
    assert info.home == str(home.resolve())
    assert len(info.readings) == 2
    assert all(reading.label == ReadingLabel.PROVIDER for reading in info.readings)


@pytest.mark.parametrize("email_source", ["token", "endpoint"])
def test_enroll_falls_back_to_email_when_account_claim_is_absent(tmp_path, email_source):
    """C-1.4 C-10.2 Homes with no account id use the token or probed account email."""
    raw = _auth(account_id=None, email="probe@example.com" if email_source == "token" else None)
    info = _adapter().enroll(Credential("codex", str(_home(tmp_path, raw)), "home"))
    assert info.account_key == "codex:probe@example.com"


def test_enroll_still_probes_when_paid_claims_and_identity_exist(tmp_path):
    """C-10.2 Complete local identity does not bypass the required pre-enrollment usage probe."""
    home = _home(tmp_path)
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return 200, SAVED_WHAM

    info = CodexAdapter(opener=opener).enroll(Credential("codex", str(home), "home"))
    assert calls == [WHAM_USAGE_URL]
    assert len(info.readings) == 2


def test_enroll_missing_identity_fails_with_actionable_fix(tmp_path):
    """C-1.4 C-10.2 Enrollment cannot invent an account key when local and server identities are absent."""
    home = _home(tmp_path, _auth(account_id=None, email=None))
    with pytest.raises(AdapterError) as caught:
        _adapter({}).enroll(Credential("codex", str(home), "home"))
    assert caught.value.code == 7
    assert caught.value.fix


@pytest.mark.parametrize("error", [
    {"code": "organization_deactivated"},
    {"message": "This organisation is blocked"},
    {"message": "organization_id is disabled"},
])
def test_explicit_organization_block_on_403_is_auth_dead(tmp_path, error):
    """C-9.3 C-23.44: explicit organization-block evidence disables a lane even on HTTP 403."""
    home = _home(tmp_path)
    before = (home / "auth.json").read_bytes()
    adapter = CodexAdapter(opener=lambda request, timeout: (403, json.dumps({"error": error}).encode()))
    verdict = adapter.probe_status(_lane(home), {})
    assert verdict["status"] == "auth-dead"
    assert verdict["readings"] == ()
    assert (home / "auth.json").read_bytes() == before


def test_organization_block_takes_precedence_over_expired_access_token(tmp_path):
    """C-9.2 C-9.3 C-23.47: explicit organization denial cannot be healed as ordinary expiry."""
    raw = _auth()
    raw["tokens"]["access_token"] = _jwt({"exp": 1})
    adapter = CodexAdapter(opener=lambda request, timeout: (401, b'{"error":{"code":"organization_deactivated"}}'))
    assert adapter.probe_status(_lane(_home(tmp_path, raw)), {})["status"] == "auth-dead"


def test_unexplained_403_is_not_authentication_death(tmp_path):
    """C-9.3: HTTP 403 without explicit authentication evidence does not disable the lane."""
    adapter = CodexAdapter(opener=lambda request, timeout: (403, b'{"error":{"code":"forbidden"}}'))
    assert adapter.probe_status(_lane(_home(tmp_path)), {})["status"] == "http-error"
