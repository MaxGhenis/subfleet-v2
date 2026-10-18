"""Round-two policy-default and documentation regressions."""
from pathlib import Path

import pytest

from tests.unit.test_conversation_service import svc  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("hard_claude", [False, True])
def test_active_astra_only_catalog_publishes_fallback(svc, hard_claude):
    policy = {"models": {"astra": {"provider": "codex", "id": "gpt-6-astra"}}}
    if hard_claude:
        policy["models"]["opus"] = {"provider": "claude", "id": "claude-opus-5-5"}
        policy.update(tiers=["hard"], chains={"build": ["opus"]})
    svc.daemon.policy = policy
    result = svc.handle("models.list", {"provider": "codex"}, None)
    assert result["default_models"] == {"codex": "gpt-6-astra"}
    assert result["models"][0]["retired"] is False


@pytest.mark.parametrize("retired_key", ["astra", "gpt-6-astra"])
def test_policy_retirement_excludes_astra_from_fallback(svc, retired_key):
    svc.daemon.policy = {"models": {"astra": {"provider": "codex", "id": "gpt-6-astra"}},
                         "retired": {retired_key: "replacement"}}
    result = svc.handle("models.list", {"provider": "codex"}, None)
    assert result["default_models"] == {} and result["models"][0]["retired"] is True


def test_design_describes_the_shipped_policy_without_claiming_astra_retired():
    design = (ROOT / "docs/desktop/design.md").read_text()
    assert "(shipped policy: `gpt-6-astra`)" in design
    assert "Retired `gpt-6-astra` is never a default" not in design


def test_pr_report_does_not_claim_the_reverted_policy_is_still_shipped():
    report = (ROOT / "docs/reports/2026-10-03-app-cutover.md").read_text()
    assert "The shipped routing policy now defines `sol = gpt-6.1-sol`" not in report
    assert "`subfleet/default_policy.json` retires Astra routing" not in report
