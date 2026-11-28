"""Review mutations must trigger the original regression AssertionErrors.

Each mutation is restored by monkeypatch; production files stay unchanged.
"""
from __future__ import annotations

import inspect
import textwrap

import pytest

from subfleet.adapters import codex
from tests.unit import test_reset_credit_claim_timing as timing
from tests.unit import test_reset_credit_properties as properties
from tests.unit import test_reset_credit_review_r2 as regressions
from tests.unit import test_reset_credit_review_r3 as redirects
from tests.unit.test_reset_credit_properties import fake_transport_clock  # noqa: F401


def _mutate_adapter(monkeypatch, predicate):
    source = textwrap.dedent(inspect.getsource(codex.CodexAdapter._request))
    old = 'outcome = "refused" if refused else "unknown"'
    assert source.count(old) == 1
    namespace = {}
    exec(compile(source.replace(old, predicate), "<review-r3 adapter mutation>", "exec"),
         codex.__dict__, namespace)
    monkeypatch.setattr(codex.CodexAdapter, "_request", namespace["_request"])


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_mutant_3xx_failed_breaks_original_redirect_regression(tmp_path, monkeypatch, status):
    _mutate_adapter(monkeypatch, 'outcome = "refused" if refused or 300 <= status < 400 else "unknown"')
    print("MUTANT 3xx-failed: original test_redirect_with_reset_body_blocks_another_consume", status)
    with pytest.raises(AssertionError):
        regressions.test_redirect_with_reset_body_blocks_another_consume(tmp_path, status)


def test_mutant_count_unsubmitted_breaks_original_full_claim_regression(monkeypatch):
    source = inspect.getsource(properties.play)
    old = '                    if str(number) not in submitted_accounts:\n                        continue\n'
    assert source.count(old) == 1
    namespace = {}
    exec(compile(source.replace(old, ""), "<review-r3 oracle mutation>", "exec"),
         properties.__dict__, namespace)
    monkeypatch.setattr(properties, "play", namespace["play"])
    monkeypatch.setattr(timing, "play", namespace["play"])
    print("MUTANT count-unsubmitted: original test_full_delayed_claim_has_no_submission_and_no_interval_violation")
    with pytest.raises(AssertionError):
        timing.test_full_delayed_claim_has_no_submission_and_no_interval_violation(monkeypatch)


@pytest.mark.parametrize("body", [
    b'{"error":{"code":"already_redeemed"},"extra":1}',
    b'not JSON',
], ids=["extra-field", "non-json"])
def test_mutant_any_409_breaks_original_outcome_table(tmp_path, monkeypatch, body):
    _mutate_adapter(monkeypatch, 'outcome = "refused" if refused or status == 409 else "unknown"')
    print("MUTANT any-409: original test_http_outcome_table_blocks_unless_definitely_unspent", body)
    with pytest.raises(AssertionError):
        regressions.test_http_outcome_table_blocks_unless_definitely_unspent(tmp_path, 409, body, "unknown")


@pytest.mark.parametrize("redirect", [302, 303])
@pytest.mark.parametrize("refusal", [409, 429])
def test_mutant_follow_redirects_breaks_original_transport_regression(tmp_path, monkeypatch, redirect, refusal):
    monkeypatch.setattr(codex._NoConsumeRedirect, "redirect_request",
                        codex.urllib.request.HTTPRedirectHandler.redirect_request)
    with pytest.raises(AssertionError):
        redirects.test_default_transport_redirect_cannot_turn_a_spend_into_failed(
            tmp_path, monkeypatch, redirect, refusal)
