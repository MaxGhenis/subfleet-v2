"""C-31.1 phone front door, policy defaults and headless gateway peer boundary."""

import json
from pathlib import Path

import pytest

from subfleet import cli, policy, protocol
from subfleet.conversations.peers import Proc, judge


@pytest.mark.parametrize("owned,code", [(True, 0), (False, 1)])
def test_owns_returns_an_authoritative_status(daemon, capsys, owned, code):
    server = daemon({"phone.owns": lambda req: {"owned": owned}})
    assert cli.main(["phone", "owns", "123"]) == code
    assert capsys.readouterr().out.strip() == ("owned" if owned else "unknown")
    assert server.args("phone.owns") == {"telegram_message_id": 123}


def test_owns_errors_cannot_fall_through_to_cos_decisions(daemon, capsys):
    daemon({"phone.owns": lambda req: protocol.fail(req.id, 1, "store failed")})
    assert cli.main(["phone", "owns", "123"]) == 69
    assert "store failed" in capsys.readouterr().err


def test_phone_does_not_start_an_offline_daemon(root, capsys):
    assert cli.main(["phone", "tap", "sf:token:allow"]) == 69
    assert "subfleet phone" in capsys.readouterr().err
    assert not (root / "daemon.pid").exists()


def test_reply_preserves_literal_text_and_update_id(daemon, capsys):
    server = daemon({"phone.reply": lambda req: {"route": "queued", "blocked_by": "person"}})
    body = '--model wrong; $(touch /never)\n"hello"'
    assert cli.main(["phone", "reply", "123", "--update-id", "77", "--", body]) == 0
    assert server.args("phone.reply") == {"telegram_message_id": 123, "text": body, "update_id": "77"}
    assert "Waiting on person" in capsys.readouterr().out


def test_tap_and_notify_are_socket_operations(daemon, capsys):
    server = daemon({"phone.tap": lambda req: {"duplicate": True}, "phone.notify": lambda req: {"enabled": False}})
    assert cli.main(["phone", "tap", "sf:abc:a0"]) == 0
    assert "Already recorded" in capsys.readouterr().out
    assert server.args("phone.tap") == {"data": "sf:abc:a0"}
    assert cli.main(["phone", "notify", "conversation", "--off", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": False}
    assert server.args("phone.notify") == {"conversation_id": "conversation", "enabled": False}


def load_phone(tmp_path, section):
    data = json.loads(policy.DEFAULT_POLICY_PATH.read_text())
    if section is None:
        data.pop("phone", None)
    else:
        data["phone"] = section
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    return policy.load_policy(path)["phone"]["telegram"]


def test_phone_policy_defaults_and_opt_in_done(tmp_path):
    assert load_phone(tmp_path, None) == {"enabled": True, "events": ["approvals", "questions", "blocks"]}
    assert load_phone(tmp_path, {"telegram": {"enabled": False, "events": ["done"]}}) == {
        "enabled": False, "events": ["done"]}
    assert load_phone(tmp_path, {"telegram": {"events": []}})["events"] == []


@pytest.mark.parametrize("value", [False, {"telegram": False}, {"telegram": {"enabled": "on"}},
                                     {"telegram": {"events": "approvals"}}, {"telegram": {"events": ["all"]}},
                                     {"telegram": {"events": ["blocks", "blocks"]}},
                                     {"telegram": {"events": [{}]}}, {"telegram": {"urgent": True}}])
def test_phone_policy_refuses_invalid_values(tmp_path, value):
    with pytest.raises(policy.PolicyError, match="phone"):
        load_phone(tmp_path, value)


GATEWAY = "/gateway/bin/tg-poller"


def verdict(parent, *, ancestors=()):
    return judge(10, chain=lambda pid: [Proc(10, 9, "??", "python3 /bin/subfleet phone tap sf:abc:allow"),
                                      parent, *ancestors],
                 executable=lambda pid: "/bin/python3", gateway_script=GATEWAY)


def test_only_exact_immediate_gateway_parent_is_person():
    assert verdict(Proc(9, 1, "??", f"python3 {GATEWAY} HOME=/fixture")).person
    assert verdict(Proc(9, 1, "??", f"/framework/Python {GATEWAY}")).person
    for command in (f"python3 unrelated.py {GATEWAY}", f"python3 -c '{GATEWAY}'",
                    f"python3 {GATEWAY}.evil", f"python3 script.py POLLER={GATEWAY}"):
        assert not verdict(Proc(9, 1, "??", command)).person
    assert not verdict(Proc(8, 1, "??", f"python3 {GATEWAY}")).person


@pytest.mark.parametrize("environment", ["LABEL=Max's", 'LABEL=one"two', "LABEL=trailing\\"])
def test_gateway_environment_is_not_parsed_as_shell_syntax(environment):
    assert verdict(Proc(9, 1, "??", f"python3 {GATEWAY} {environment}")).person
    assert not verdict(Proc(9, 1, "??", f"python3 unrelated.py {GATEWAY} {environment}")).person
    assert not verdict(Proc(9, 1, "??", "python3")).person


@pytest.mark.parametrize("marker", ["SUBFLEET_JOB=j", "SUBFLEET_ATTEMPT=a", "subfleet.guardian"])
def test_gateway_never_overrides_an_agent_ancestor(marker):
    assert not verdict(Proc(9, 8, "??", f"python3 {GATEWAY}"),
                       ancestors=[Proc(8, 1, "??", marker)]).person
