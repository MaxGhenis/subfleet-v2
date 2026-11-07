"""C-31.1, C-31.4: `lanes enroll --api <email> [--credit-usd N]` reaches the daemon as a
`claude-api-<email>` reference; the CLI refuses what the daemon would."""

from __future__ import annotations

import pytest

from tests.unit.test_cli import run_cli


def enroll_answer(request):
    credential = request.args.get("credential")
    return {"lanes": [], "enrolled": {
        "lane_id": "claude-api-1", "provider": "claude-api", "owner": "v2",
        "account_key": "claude-api:" + credential.removeprefix("claude-api-"),
        "label": credential.removeprefix("claude-api-"), "home": None,
        "credit_usd": request.args.get("credit_usd") or 200.0}}


@pytest.mark.parametrize("argv,credential,credit", [
    (["lanes", "enroll", "--api", "plan@example.org"], "claude-api-plan@example.org", None),
    (["lanes", "enroll", "--api", "claude-api-plan@example.org"], "claude-api-plan@example.org", None),
    (["lanes", "enroll", "--api", "plan@example.org", "--credit-usd", "100"], "claude-api-plan@example.org", 100.0),
    (["lanes", "enroll", "claude-api-plan@example.org", "--credit-usd", "62.5"], "claude-api-plan@example.org", 62.5),
])
def test_api_enrollment_reaches_the_daemon_as_a_credit_reference(daemon, capsys, argv, credential, credit):
    server = daemon({"lanes": enroll_answer})
    assert run_cli(argv) == 0
    sent = server.args("lanes")
    assert sent["action"] == "enroll" and sent["credential"] == credential
    assert sent.get("credit_usd") == credit
    out = capsys.readouterr().out
    assert "claude-api-1  claude-api" in out and f"credit=${credit or 200:g}/cycle" in out


@pytest.mark.parametrize("argv,message", [
    (["lanes", "enroll", "--api", "not-an-email"], "name the plan's email"),
    (["lanes", "enroll", "--api", "   "], "name the plan's email"),
    (["lanes", "enroll", "/tmp/some-home", "--credit-usd", "50"], "applies only to an API credit lane"),
    (["lanes", "enroll", "claude-quota-x@example.org", "--credit-usd", "50"], "applies only to an API credit lane"),
    (["lanes", "enroll", "--api", "plan@example.org", "--credit-usd", "0"], "positive number"),
    (["lanes", "enroll", "--api", "plan@example.org", "--credit-usd", "-1"], "positive number"),
    (["lanes", "enroll", "--api", "plan@example.org", "--credit-usd", "nan"], "positive number"),
    (["lanes", "enroll", "--api", "plan@example.org", "--credit-usd", "inf"], "positive number"),
])
def test_the_cli_refuses_a_bad_credit_enrollment_before_the_daemon(daemon, capsys, argv, message):
    server = daemon({"lanes": enroll_answer})
    assert run_cli(argv) == 2
    assert message in capsys.readouterr().err
    with pytest.raises(AssertionError, match="no 'lanes' request was made"):
        server.args("lanes")
