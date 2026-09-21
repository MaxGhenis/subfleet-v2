"""The E2E transport accepts only synthetic identity-bound usage requests."""
import base64
import json
import urllib.request

import pytest

from subfleet.adapters.codex import WHAM_USAGE_URL, WHAM_RESET_CREDITS_CONSUME_URL
from tests.fake import codex_http


def request(*, account='fake-1', claimed='fake-1', url=WHAM_USAGE_URL, token=None):
    claims = {'https://api.openai.com/auth': {'chatgpt_account_id': claimed}}
    token = token or ('fixture.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.fixture')
    return urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'chatgpt-account-id': account})


def test_codex_http_fixture_returns_real_usage_payload():
    status, raw = codex_http.opener(request(), 1)
    payload = json.loads(raw)
    assert status == 200 and payload['rate_limit']['secondary_window']['used_percent'] == 20
    assert payload['rate_limit_reset_credits']['available_count'] == 0


@pytest.mark.parametrize('changes', [dict(account='fake-2'), dict(claimed='other'),
                                    dict(token='not-a-fixture'), dict(url=WHAM_RESET_CREDITS_CONSUME_URL),
                                    dict(url='https://example.test/unexpected')])
def test_codex_http_fixture_rejects_unexpected_requests(changes):
    with pytest.raises(AssertionError):
        codex_http.opener(request(**changes), 1)


def test_e2e_fallback_transport_fails_closed():
    with pytest.raises(AssertionError, match='real HTTP transport'):
        codex_http.reject_network(request(), timeout=1)
