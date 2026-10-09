"""C-10.5: no credential in anything an identity check keeps (review of #159, findings 1, 5).

Written by the independent review of PR #159 (Subfleet job
20261009-100051-pr159-review-r1, GPT-6.1 Sol), each a failing reproduction of one
finding before its fix; kept as regressions. Finding numbers are the review's.
"""

import http.client
import json
from types import SimpleNamespace

import pytest

from subfleet.adapters.claude import ClaudeAdapter, ORG_UNAVAILABLE
from subfleet.daemon import Daemon
from subfleet.store import Store


TOKEN = "review-synthetic-bearer-0123456789abcdef"
ENV = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}


def echoed_profile(field):
    payload = {
        "account": {"email": "owner@example.com", "uuid": "account-1"},
        "organization": {"uuid": "organization-1", "organization_type": "claude_max"},
    }
    container, name = field.split(".")
    payload[container][name] = f"echo-{TOKEN}"
    return ClaudeAdapter(profile_opener=lambda *_: (200, json.dumps(payload).encode()))


@pytest.mark.parametrize("field", ["account.email", "account.uuid", "organization.uuid", "organization.organization_type"])
def test_echoed_profile_fields_never_reach_identity_evidence(field):
    """C-10.5 a profile field that echoes the credential is refused, never kept (finding 1)."""
    adapter = echoed_profile(field)
    check = adapter.identity_check(None, "owner@example.com", ENV)
    assert TOKEN not in json.dumps(check.evidence()), "credential was copied into identity evidence"


def test_echoed_organization_type_never_reaches_desktop_event(tmp_path):
    """C-10.5, C-10.3 nor stored in a desktop.identity event (finding 1)."""
    adapter = echoed_profile("organization.organization_type")
    profile = adapter.probe_profile(ENV)
    with Store(tmp_path / "state.sqlite3") as store:
        daemon = SimpleNamespace(store=store, _desktop_profile=lambda _: profile)
        Daemon._desktop_identity(daemon)
        events = store.query("SELECT data_json FROM events WHERE kind='desktop.identity'")
        assert TOKEN not in json.dumps(events), "credential was stored in a desktop.identity event"


def test_organization_bad_status_line_does_not_expose_credential():
    """C-10.5 any transport failure is reduced to its type (finding 5)."""
    def bad_response(*_):
        raise http.client.BadStatusLine(f"Authorization: Bearer {TOKEN}")

    adapter = ClaudeAdapter(org_opener=bad_response)
    try:
        result = adapter.probe_org(ENV, refresh=True)
    except Exception as error:
        assert TOKEN not in str(error), "credential escaped in a transport exception"
        pytest.fail(f"organization transport exception escaped: {type(error).__name__}")
    assert result.status == ORG_UNAVAILABLE
