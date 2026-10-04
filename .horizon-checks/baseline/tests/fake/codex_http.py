"""Local subscription HTTP responses for end-to-end fake Codex accounts.

Keep the real adapter, identity parser, timers, and response validation. Only
the HTTP transport is replaced; an unexpected account or endpoint fails closed.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from subfleet.adapters.codex import WHAM_USAGE_URL


def opener(request, timeout):
    if request.full_url != WHAM_USAGE_URL or request.get_method() != 'GET':
        raise AssertionError('unexpected fake Codex HTTP endpoint or method')
    token = request.get_header('Authorization', '').removeprefix('Bearer ')
    parts = token.split('.')
    if len(parts) != 3 or parts[0] != 'fixture' or parts[2] != 'fixture':
        raise AssertionError('Codex HTTP fixture refuses a non-fixture credential')
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
    account = claims.get('https://api.openai.com/auth', {}).get('chatgpt_account_id')
    if not isinstance(account, str) or not account.startswith('fake-') or request.get_header('Chatgpt-account-id') != account:
        raise AssertionError('Codex HTTP fixture account binding disagrees')
    now = datetime.now(timezone.utc)
    return 200, json.dumps({
        'plan_type': 'plus',
        'rate_limit': {'allowed': True, 'limit_reached': False,
                       'primary_window': {'window_minutes': 300, 'used_percent': 10,
                                          'reset_at': int((now + timedelta(hours=5)).timestamp())},
                       'secondary_window': {'window_minutes': 10080, 'used_percent': 20,
                                            'reset_at': int((now + timedelta(days=7)).timestamp())}},
        'rate_limit_reset_credits': {'available_count': 0, 'applicable_available_count': 0},
    }).encode()


def install() -> None:
    from subfleet.adapters.codex import CodexAdapter
    original = CodexAdapter.__init__

    def initialize(self, *args, **kwargs):
        kwargs['opener'] = kwargs.get('opener') or opener
        original(self, *args, **kwargs)

    CodexAdapter.__init__ = initialize


def reject_network(*args, **kwargs):
    raise AssertionError('end-to-end fixtures must not use the real HTTP transport')
