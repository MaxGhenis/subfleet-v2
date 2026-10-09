"""C-10.5, C-10.6, D-ID1: what a setup token can say about itself.

On 2026-10-09 every lane's setup token answered the profile endpoint 403 ("OAuth
token does not meet scope requirement any_of(user:profile, user:office)"), and
`claude auth status` said only `authMethod: oauth_token`. `GET /v1/models`
answered 200 (403 for six of the sixteen) with `anthropic-organization-id`: the
organization the token belongs to, at no model cost. These tests hold the adapter
to that, through an opener that stands in for the network, and to never letting
the token reach anything it returns.
"""

from __future__ import annotations

import json
import urllib.error
from email.message import Message

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.adapters import claude as claude_module
from subfleet.adapters.claude import (
    ORG_HEADER, ORG_INVALID, ORG_OK, ORG_PROBE_URL, ORG_UNAVAILABLE, PROFILE_NO_SCOPE,
    ClaudeAdapter, ProfileResult,
)
from subfleet.contracts import IdentityStatus
from tests.conftest import NOW, org_opener

#: The real opener, taken before the autouse `no_network` fixture swaps in a stand-in.
REAL_URLOPEN_ORG = claude_module._urlopen_org

ORG = "5ba7ed00-1111-4000-8000-00000000d1d1"
OTHER = "9a1a9a1a-2222-4000-8000-000000000002"
TOKEN = "fixture-oauth-token-1"
ENV = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}


def no_scope(request, timeout):
    """The profile endpoint as it answered every setup token on 2026-10-09."""
    return 403, b""


def adapter(opener=None, *, profile=no_scope, **kwargs) -> ClaudeAdapter:
    return ClaudeAdapter(now=lambda: NOW, profile_opener=profile,
                         org_opener=opener or org_opener(200, ORG), **kwargs)


def test_d_id1_the_organization_probe_asks_models_with_the_lanes_own_bearer():
    """C-10.6 the identity is what the token's own answer says: the request is
    `GET /v1/models` with that bearer, the API version and the OAuth beta."""
    seen = []

    def watchful(request, timeout):
        seen.append((request.full_url, request.get_method(), dict(request.header_items()), timeout))
        return 200, ORG

    result = adapter(watchful).probe_org(ENV)
    assert result.status == ORG_OK and result.org_uuid == ORG and result.identity == f"org:{ORG}"
    url, method, headers, timeout = seen[0]
    assert url == ORG_PROBE_URL and method == "GET" and timeout == 15.0
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Anthropic-version"] == "2023-06-01" and headers["Anthropic-beta"] == "oauth-2025-04-20"


@pytest.mark.parametrize("status, header, expected", [
    (200, ORG, ORG_OK),
    (403, ORG, ORG_OK),                  # refused, and still names the organization (6 of 16 on 10/9)
    (429, ORG, ORG_OK),
    (401, ORG, ORG_UNAVAILABLE),         # unauthenticated: nothing it says is the token's
    (500, ORG, ORG_UNAVAILABLE),
    (200, None, ORG_UNAVAILABLE),
    (200, "", ORG_UNAVAILABLE),
    (200, "not:an-org", ORG_INVALID),
    (200, "x" * 200, ORG_INVALID),
    (200, TOKEN, ORG_INVALID),           # an echo of the credential is never an identity
])
def test_d_id1_what_each_answer_means(status, header, expected):
    result = adapter(org_opener(status, header)).probe_org(ENV)
    assert result.status == expected
    assert (result.identity is not None) == (expected == ORG_OK)


def test_d_id1_the_real_opener_reads_the_header_from_an_error_response_too(monkeypatch):
    """C-10.6 some tokens get 403 that still names the organization; `_urlopen_org` reads the header of
    the HTTPError and closes it."""
    headers = Message()
    headers[ORG_HEADER] = ORG
    closed = []

    class Refused(urllib.error.HTTPError):
        def close(self):
            closed.append(True)

    def refuse(request, timeout):
        raise Refused(request.full_url, 403, "forbidden", headers, None)

    monkeypatch.setattr(claude_module.urllib.request, "urlopen", refuse)
    status, header = REAL_URLOPEN_ORG(claude_module.urllib.request.Request(ORG_PROBE_URL), 1.0)
    assert (status, header, closed) == (403, ORG, [True])


def test_d_id1_an_answer_is_kept_across_adapters_and_a_failure_is_not():
    """C-10.6 a token's organization never changes, and the timers build an
    adapter per read: a good answer serves later adapters without a request; a
    failed one is asked again; `refresh` always asks; another token is its own."""
    calls = []

    def counting(status, header):
        def opener(request, timeout):
            calls.append(request.headers.get("Authorization"))
            return status, header
        return opener

    assert adapter(counting(500, None)).probe_org(ENV).status == ORG_UNAVAILABLE
    assert adapter(counting(200, ORG)).probe_org(ENV).ok
    assert adapter(counting(200, OTHER)).probe_org(ENV).org_uuid == ORG          # cached
    assert len(calls) == 2
    assert adapter(counting(200, OTHER)).probe_org(ENV, refresh=True).org_uuid == OTHER
    assert adapter(counting(200, OTHER)).probe_org({"CLAUDE_CODE_OAUTH_TOKEN": "another"}).org_uuid == OTHER
    assert len(calls) == 4


@pytest.mark.parametrize("recorded, label, org, expected", [
    (None, "max@hivesight.ai", ORG, IdentityStatus.ENROLLED),          # learns org:<ORG>
    (f"org:{ORG}", "max@hivesight.ai", ORG, IdentityStatus.ENROLLED),  # the binding holds
    (f"org:{ORG}", "max@hivesight.ai", OTHER, IdentityStatus.MISMATCH),  # the token changed accounts
    (f"org:{ORG}", "max@hivesight.ai", None, IdentityStatus.UNVERIFIED),
    (None, "max@hivesight.ai", None, IdentityStatus.ENROLLED),         # as before D-ID1: label only
])
def test_c10_6_a_setup_tokens_identity_check_runs_on_its_organization(recorded, label, org, expected):
    """C-10.6 profile 403, then the organization header: it can hold or break the
    binding, never prove the label (that is `lane_identity.judge`'s)."""
    check = adapter(org_opener(200 if org else 500, org)).identity_check(recorded, label, ENV)
    assert check.status is expected
    assert check.profile_status == PROFILE_NO_SCOPE
    if org:
        assert check.observed == f"org:{org}" and check.source == "org-header"
        assert check.evidence()["observed"] == f"org:{org}"
        assert check.evidence()["identity"]["org_uuid"] == org


@pytest.mark.parametrize("email, expected", [("max@hivesight.ai", IdentityStatus.VERIFIED),
                                             ("max@maxghenis.com", IdentityStatus.MISMATCH)])
def test_c10_6_an_organization_record_answered_by_a_profile_is_judged_on_the_label(email, expected):
    """C-10.6 a lane that recorded only its organization, whose credential can now
    read its profile: the same organization, and the label decides."""
    body = json.dumps({"account": {"email": email, "uuid": "acct-1"},
                       "organization": {"uuid": ORG, "organization_type": "claude_max"}}).encode()
    made = adapter(profile=lambda request, timeout: (200, body))
    check = made.identity_check(f"org:{ORG}", "max@hivesight.ai", ENV)
    assert check.status is expected and check.org_type == "claude_max"
    assert check.source == "profile" and check.observed == f"acct-1:{ORG}"


tokens = st.text(st.characters(codec="ascii", categories=("L", "N"), include_characters="-_"),
                 min_size=12, max_size=60)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tokens, st.sampled_from(["oserror", "valueerror", "httperror", "header-echo", "header-wraps",
                                "ok", "status-500"]))
def test_c10_5_no_answer_or_failure_ever_carries_the_token(token, behaviour):
    """C-10.5 whatever the endpoint does, including failing with the bearer in
    the exception's text or echoing it in the header, the organization result,
    the identity check and its evidence never contain the token."""
    def opener(request, timeout):
        if behaviour == "oserror":
            raise OSError(f"connect failed: Authorization: Bearer {token}")
        if behaviour == "valueerror":
            raise ValueError(token)
        if behaviour == "httperror":
            raise urllib.error.HTTPError(f"https://x/{token}", 500, token, Message(), None)
        if behaviour == "header-echo":
            return 200, token
        if behaviour == "header-wraps":
            return 200, f"x{token}x"
        if behaviour == "status-500":
            return 500, ORG
        return 200, ORG

    claude_module.forget_org_cache()
    env = {"CLAUDE_CODE_OAUTH_TOKEN": token}
    made = adapter(opener)
    result = made.probe_org(env, refresh=True)
    check = made.identity_check(None, "max@hivesight.ai", env)
    text = json.dumps([result.__dict__, check.__dict__, check.evidence()], default=str)
    assert token not in text
