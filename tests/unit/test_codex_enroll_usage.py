"""C-10.2, C-10.8: Codex enrolment judges its own usage read as the probe cycle does.

Codex enrolment used to read `auth.json`, ask the usage endpoint, and keep whatever
came back, so a home whose token the endpoint refused was enrolled and disabled by
the next probe cycle; an automatic re-check that relied on it would restore such a
lane over and over. Now one verdict (`CodexAdapter._usage_verdict`) serves both:
enrolment refuses what the probe would call `auth-dead` or `revoked`, reports what it
saw as `usage_status`, and the probe is unchanged.
"""
from __future__ import annotations

import json

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.adapters.codex import CodexAdapter, WHAM_USAGE_URL
from subfleet.contracts import Credential
from tests.unit.test_codex_probe import NOW, SAVED_WHAM, _auth, _home, _lane


def adapter(status, body=SAVED_WHAM, seen=None):
    def opener(request, timeout):
        if seen is not None:
            seen.append((request.full_url, timeout))
        return status, body
    return CodexAdapter(opener=opener, now=lambda: NOW)


def credential(home):
    return Credential("codex", str(home), "home")


@pytest.mark.parametrize("status,body", [
    (401, json.dumps({"error": {"code": "invalid_api_key", "message": "Incorrect API key provided"}}).encode()),
    (403, json.dumps({"error": {"code": "account_deactivated",
                                "message": "Your organization has been deactivated"}}).encode()),
    (401, json.dumps({"error": {"code": "refresh_token_revoked",
                                "message": "refresh token was revoked"}}).encode()),
])
def test_c10_2_enrolment_refuses_a_token_the_probe_would_disable(tmp_path, status, body):
    home = _home(tmp_path)
    with pytest.raises(AdapterError) as caught:
        adapter(status, body).enroll(credential(home))
    assert caught.value.code == 5
    assert "refused the home's token" in str(caught.value)
    probe = adapter(status, body).probe_status(_lane(home), {"CODEX_HOME": str(home)})
    assert probe["status"] in ("auth-dead", "revoked")


def test_c10_2_enrolment_reports_what_the_usage_endpoint_said(tmp_path):
    home = _home(tmp_path)
    seen = []
    info = adapter(200, seen=seen).enroll(credential(home))
    assert info.usage_status == "ok" and info.readings
    assert seen == [(WHAM_USAGE_URL, seen[0][1])]                   # one usage read, no more
    limited = json.loads(SAVED_WHAM)
    limited["rate_limit"]["limit_reached"] = True
    assert adapter(200, json.dumps(limited).encode()).enroll(credential(home)).usage_status == "limited"


@pytest.mark.parametrize("status", [500, 502])
def test_c10_2_an_operator_may_still_enrol_while_the_endpoint_is_down(tmp_path, status):
    """No answer is no evidence either way: enrolment accepts, and says so, and the
    automatic path (which needs `ok` or `limited`) will not take it."""
    info = adapter(status, b"").enroll(credential(_home(tmp_path)))
    assert info.usage_status == "network-error" and info.readings == ()


def test_c10_2_an_expired_token_enrols_as_before(tmp_path):
    """C-23.47: a token past its expiry is the CLI's to refresh; enrolment accepts it."""
    raw = _auth()
    home = _home(tmp_path, raw)
    info = adapter(401, json.dumps({"error": {"code": "token_expired", "message": "token expired"}}).encode()) \
        .enroll(credential(home))
    assert info.usage_status == "expired-token"


def test_c9_3_the_probe_is_unchanged_by_the_shared_verdict(tmp_path):
    home = _home(tmp_path)
    ok = adapter(200).probe_status(_lane(home), {"CODEX_HOME": str(home)})
    assert ok["status"] == "ok" and ok["readings"] and ok["account_key"] == "codex:account-test"
    down = adapter(503, b"").probe_status(_lane(home), {"CODEX_HOME": str(home)})
    assert down["status"] == "network-error" and down["readings"] == ()
