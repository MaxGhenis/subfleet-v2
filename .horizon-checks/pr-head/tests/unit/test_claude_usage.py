"""C-9.9: the usage-endpoint sensor, parsed from the payload shape observed on 2026-09-06."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import Message

import pytest

from subfleet.adapters import claude as claude_module
from subfleet.adapters.claude import (
    OAUTH_USAGE_URL, SOURCE_OAUTH_USAGE, ClaudeAdapter, IdentityCheck, IdentityStatus, UsageResult,
    home_login, keychain_service_for_home, login_expired,
)
from subfleet.contracts import Credential, Lane, LaneOwner, ReadingLabel

PAYLOAD = {
    "five_hour": {"utilization": 94.0, "resets_at": "2026-09-06T12:10:00.405212+00:00"},
    "seven_day": {"utilization": 88.0, "resets_at": "2026-09-10T16:00:00.405231+00:00"},
    "seven_day_opus": None, "seven_day_sonnet": None, "nimbus_quill": {"utilization": 0.0, "resets_at": None},
    "extra_usage": {"utilization": None},
    "limits": [
        {"kind": "session", "group": "session", "percent": 94, "severity": "critical",
         "resets_at": "2026-09-06T12:10:00.405212+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 88, "severity": "warning",
         "resets_at": "2026-09-10T16:00:00.405231+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 24, "severity": "normal",
         "resets_at": "2026-09-10T16:00:00.405423+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}, "is_active": False},
    ],
    "spend": {"used": {"amount_minor": 0, "currency": "USD", "exponent": 2}, "limit": None},
}
NOW = datetime(2026, 9, 6, 12, 5, tzinfo=timezone.utc)


def lane():
    return Lane("claude-3", "claude", "claude:uuid-a:uuid-o", Credential("claude", "SF_TOKEN", "env"),
                None, LaneOwner.V2, False, True, identity="uuid-a:uuid-o")


def adapter(opener, *, binds=True):
    made = ClaudeAdapter(usage_opener=opener, now=lambda: NOW)
    status = IdentityStatus.VERIFIED if binds else IdentityStatus.MISMATCH
    made.lane_identity_check = lambda lane, env: IdentityCheck(status, "ok", "x", "y", None, None, None, "t")
    return made


def ok_opener(request, timeout):
    assert request.full_url == OAUTH_USAGE_URL
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert request.get_header("Anthropic-beta") == "oauth-2025-04-20"
    return 200, json.dumps(PAYLOAD).encode()


def http_error(code, headers=None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    def opener(request, timeout):
        raise urllib.error.HTTPError(OAUTH_USAGE_URL, code, "err", message, None)
    return opener


def test_c9_9_windows_become_provider_readings_with_the_fable_week_scoped_to_its_model():
    result = adapter(ok_opener).probe_usage(lane(), {"CLAUDE_CODE_OAUTH_TOKEN": "secret-token"})
    assert result.status == "ok" and result.limit_reached is False
    rows = {(r.scope, r.window): r for r in result.readings}
    assert set(rows) == {("account", "five_hour"), ("account", "seven_day"), ("claude-fable-5-1", "seven_day")}
    assert rows[("account", "five_hour")].utilization == pytest.approx(.94)
    assert rows[("account", "seven_day")].utilization == pytest.approx(.88)
    assert rows[("account", "seven_day")].resets_at == "2026-09-10T16:00:00Z"
    fable = rows[("claude-fable-5-1", "seven_day")]
    assert fable.utilization == pytest.approx(.24) and fable.label is ReadingLabel.PROVIDER
    assert fable.source == SOURCE_OAUTH_USAGE and fable.observed_at == "2026-09-06T12:05:00Z"
    assert all(r.lane_id == "claude-3" and r.attempt_id is None for r in result.readings)


def test_c9_9_the_probe_status_shape_is_what_the_timer_stores():
    probe = adapter(ok_opener).probe_status(lane(), {"CLAUDE_CODE_OAUTH_TOKEN": "secret-token"})
    assert probe["status"] == "ok" and len(probe["readings"]) == 3 and probe["limit_reached"] is False


def test_c9_9_an_exhausted_shared_window_reports_limit_reached():
    payload = json.loads(json.dumps(PAYLOAD)); payload["seven_day"]["utilization"] = 100.0
    result = adapter(lambda r, t: (200, json.dumps(payload).encode())).probe_usage(lane(), {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert result.status == "ok" and result.limit_reached is True


def test_c9_9_rate_limited_yields_no_reading_and_the_retry_after():
    result = adapter(http_error(429, {"Retry-After": "3035"})).probe_usage(lane(), {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert result == UsageResult("rate-limited", (), None, 3035, "HTTP 429")
    probe = result.as_probe()
    assert probe["readings"] == () and probe["retry_after_s"] == 3035 and probe["status"] == "rate-limited"


@pytest.mark.parametrize("code,status", [(401, "auth-dead"), (403, "no-scope"), (500, "unavailable")])
def test_c9_9_other_errors_map_without_readings(code, status):
    result = adapter(http_error(code)).probe_usage(lane(), {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert result.status == status and result.readings == ()


def test_c9_9_a_network_failure_is_unavailable_and_never_quotes_the_request():
    def boom(request, timeout):
        raise OSError("Authorization: Bearer secret-token leaked?")
    result = adapter(boom).probe_usage(lane(), {"CLAUDE_CODE_OAUTH_TOKEN": "secret-token"})
    assert result.status == "unavailable" and result.detail == "OSError"
    assert "secret" not in json.dumps(result.as_probe())


def test_c10_6_readings_are_returned_only_when_the_identity_binds():
    result = adapter(ok_opener, binds=False).probe_usage(lane(), {"CLAUDE_CODE_OAUTH_TOKEN": "secret-token"})
    assert result.status == "identity-unbound" and result.readings == ()


def test_c9_9_no_token_is_unavailable_without_a_request():
    result = adapter(lambda r, t: (_ for _ in ()).throw(AssertionError("no request"))).probe_usage(lane(), {})
    assert result.status == "unavailable" and result.detail == "no-token"


# --- home lanes on macOS: the per-directory keychain item (C-9.9, C-23.47) ------

NOW_MS = NOW.timestamp() * 1000


def blob(expires_ms, refresh=True):
    oauth = {"accessToken": "home-access-token", "expiresAt": expires_ms, "scopes": ["user:profile"],
             "subscriptionType": "max"}
    if refresh:
        oauth["refreshToken"] = "home-refresh-token"
    return json.dumps({"claudeAiOauth": oauth})


def test_keychain_service_is_the_directory_hash_claude_code_uses(tmp_path):
    import hashlib
    home = tmp_path / "logins" / "max@farness.ai"
    assert keychain_service_for_home(home) == "Claude Code-credentials-" + hashlib.sha256(str(home).encode()).hexdigest()[:8]


def test_home_login_falls_back_to_the_keychain_item_when_the_file_is_absent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    asked = []
    def fake_blob(service, *, security_bin="security"):
        asked.append(service)
        return blob(NOW_MS + 3_600_000)
    monkeypatch.setattr(claude_module, "_keychain_blob", fake_blob)
    oauth = home_login(home)
    assert asked == [keychain_service_for_home(home)]
    assert oauth["accessToken"] == "home-access-token"
    assert ClaudeAdapter._bearer({"CLAUDE_CONFIG_DIR": str(home)}) == "home-access-token"
    (home / ".credentials.json").write_text(blob(NOW_MS + 10, refresh=False).replace("home-access-token", "file-token"))
    assert ClaudeAdapter._bearer({"CLAUDE_CONFIG_DIR": str(home)}) == "file-token"   # the file wins when present


def test_an_expired_home_login_is_expired_token_without_a_request(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(claude_module, "_keychain_blob", lambda service, **k: blob(NOW_MS - 1000))
    assert login_expired(home_login(home), NOW_MS)
    made = adapter(lambda r, t: (_ for _ in ()).throw(AssertionError("no request with an expired token")))
    result = made.probe_usage(lane(), {"CLAUDE_CONFIG_DIR": str(home)})
    assert result.status == "expired-token" and result.readings == ()


def test_a_401_on_a_home_lane_with_a_refresh_token_is_expired_token_not_auth_dead(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(claude_module, "_keychain_blob", lambda service, **k: blob(NOW_MS + 3_600_000))
    result = adapter(http_error(401)).probe_usage(lane(), {"CLAUDE_CONFIG_DIR": str(home)})
    assert result.status == "expired-token"
    monkeypatch.setattr(claude_module, "_keychain_blob", lambda service, **k: blob(NOW_MS + 3_600_000, refresh=False))
    result = adapter(http_error(401)).probe_usage(lane(), {"CLAUDE_CONFIG_DIR": str(home)})
    assert result.status == "auth-dead"


@pytest.mark.parametrize('limits', [None, {}, 'unavailable', [None], [{}],
    [{'percent': 25, 'scope': {'model': {'display_name': 'Fable'}}}],
    [{'kind': 'weekly_scoped_v2', 'percent': 25, 'scope': {'model': {'display_name': 'Fable'}}}],
    [{'kind': [], 'percent': 25}],
    [{'kind': 'weekly_scoped', 'percent': 25, 'scope': {'model': {'display_name': 'renamed-model'}}}],
    [{'kind': 'weekly_scoped', 'percent': 25, 'scope': None}],
    [{'kind': 'weekly_scoped', 'percent': None, 'scope': {'model': {'display_name': 'Fable'}}}],
    [{'kind': 'weekly_scoped', 'percent': float('nan'), 'scope': {'model': {'display_name': 'Fable'}}}],
    [{'kind': 'weekly_scoped', 'percent': 110, 'scope': {'model': {'display_name': 'Fable'}}}],
])
def test_c11_7_incomplete_usage_snapshot_cannot_release_reserved_quota(limits):
    """C-9.9, C-11.7: publish no shared reading from a broken scoped inventory."""
    payload = {**PAYLOAD, 'limits': limits}
    result = adapter(lambda *args: (200, json.dumps(payload).encode())).probe_usage(
        lane(), {'CLAUDE_CODE_OAUTH_TOKEN': 'secret-token'})
    assert result.status == 'unavailable'
    assert result.readings == ()


def test_c11_7_complete_shared_only_snapshot_releases_removed_reserved_window():
    """C-11.7: a successful later endpoint snapshot can remove an old Fable bucket."""
    from dataclasses import asdict
    from subfleet.capacity import build_view
    from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
    from subfleet.scheduler import evaluate

    earlier = asdict(adapter(ok_opener).probe_usage(lane(),
        {'CLAUDE_CODE_OAUTH_TOKEN': 'secret-token'}).readings[-1])
    earlier['observed_at'] = '2026-09-06T12:04:00Z'
    payload = {**PAYLOAD, 'five_hour': {**PAYLOAD['five_hour'], 'utilization': 10},
               'seven_day': {**PAYLOAD['seven_day'], 'utilization': 20}, 'limits': []}
    result = adapter(lambda *args: (200, json.dumps(payload).encode())).probe_usage(
        lane(), {'CLAUDE_CODE_OAUTH_TOKEN': 'secret-token'})
    assert result.status == 'ok'
    snapshot = build_view([lane()], [earlier, *result.readings], now=NOW)
    decision = evaluate(load_policy(DEFAULT_POLICY_PATH), snapshot, {'pinned_model': 'opus'})
    assert decision.chosen_lane == lane().lane_id
