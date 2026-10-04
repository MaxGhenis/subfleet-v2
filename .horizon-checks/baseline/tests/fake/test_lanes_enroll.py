"""C-10.2 `lanes enroll`, C-9.6 `lanes hold` and `release`: the daemon ops behind the CLI verbs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module
from subfleet import protocol
from subfleet.adapters.base import AdapterError
from subfleet.adapters.registry import register
from subfleet.contracts import LaneInfo, Reading, ReadingLabel
from subfleet.daemon import Daemon
from subfleet.procs import Containment
from tests.fake.conftest import Harness


class FakeClaude:
    """An adapter whose enrol turn answers from the directory name, never from a network."""

    def enroll(self, credential):
        home = Path(credential.ref)
        if credential.kind == "home" and not (home / ".claude.json").exists():
            raise AdapterError("claude: the credential did not authenticate (no login here)", code=5,
                               fix="claude auth login under this CLAUDE_CONFIG_DIR")
        email = home.name if credential.kind == "home" else credential.ref.removeprefix("claude-quota-")
        number = "".join(ch for ch in email if ch.isdigit()) or "0"
        return LaneInfo(f"claude:acct-{number}:org-{number}", "max", str(home) if credential.kind == "home" else None,
                        (Reading("", "account", "seven_day", .4, "2026-09-10T16:00:00Z", ReadingLabel.PROVIDER,
                                 "oauth-usage", "2026-09-06T21:00:00Z"),),
                        identity=f"acct-{number}:org-{number}", identity_status="verified", label=email)


@pytest.fixture
def core(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "unit-test-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: False)
    monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
    register("claude", FakeClaude)
    daemon = Daemon(root)
    try:
        yield daemon, root
    finally:
        daemon.close()


def login_dir(tmp_path, email):
    home = tmp_path / "logins" / email
    home.mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    return home


def lanes_json(root):
    payload = json.loads((root / "lanes.json").read_text())
    return payload["lanes"] if isinstance(payload, dict) else payload


def test_c10_2_enroll_a_login_directory_as_a_home_lane(core, tmp_path):
    daemon, root = core
    home = login_dir(tmp_path, "lane1@example.test")
    result = daemon.dispatch("lanes", {"action": "enroll", "credential": str(home)})
    row = result["enrolled"]
    assert row["lane_id"] == "claude-1" and row["provider"] == "claude"
    assert row["credential_kind"] == "home" and row["credential_ref"] == str(home) and row["home"] == str(home)
    assert row["owner"] == "v2" and row["enabled"] == 1 and row["desktop"] == 0
    assert row["identity"] == "acct-1:org-1" and row["label"] == "lane1@example.test"
    assert row["identity_status"] == "verified" and row["account_key"] == "claude:acct-1:org-1"
    readings = daemon.store.query("SELECT lane_id, scope, source FROM readings WHERE lane_id='claude-1'")
    assert [(r["lane_id"], r["scope"], r["source"]) for r in readings] == [("claude-1", "account", "oauth-usage")]
    seeded = [r for r in lanes_json(root) if r["lane_id"] == "claude-1"][0]
    assert seeded["credential_kind"] == "home" and seeded["home"] == str(home) and seeded["owner"] == "v2"
    kinds = [row["kind"] for row in daemon.store.list_events()]
    assert "lane.enrolled" in kinds


def test_c10_2_lane_ids_continue_from_the_store_and_owner_is_honoured(core, tmp_path):
    daemon, root = core
    daemon.dispatch("lanes", {"action": "enroll", "credential": str(login_dir(tmp_path, "lane1@example.test"))})
    second = daemon.dispatch("lanes", {"action": "enroll", "credential": str(login_dir(tmp_path, "lane2@example.test")),
                                        "owner": "v1"})
    assert second["enrolled"]["lane_id"] == "claude-2" and second["enrolled"]["owner"] == "v1"
    assert [r["lane_id"] for r in second["lanes"] if r["provider"] == "claude"] == ["claude-1", "claude-2"]


def test_c10_2_a_directory_without_a_login_and_a_duplicate_are_refused(core, tmp_path):
    daemon, root = core
    empty = tmp_path / "logins" / "nobody@example.test"
    empty.mkdir(parents=True)
    with pytest.raises(protocol.ProtocolError) as refused:
        daemon.dispatch("lanes", {"action": "enroll", "credential": str(empty)})
    assert refused.value.code == 5 and "did not authenticate" in str(refused.value)
    home = login_dir(tmp_path, "lane1@example.test")
    daemon.dispatch("lanes", {"action": "enroll", "credential": str(home)})
    with pytest.raises(protocol.ProtocolError) as duplicate:
        daemon.dispatch("lanes", {"action": "enroll", "credential": str(home)})
    assert "already lane claude-1" in str(duplicate.value)
    with pytest.raises(protocol.ProtocolError):
        daemon.dispatch("lanes", {"action": "enroll", "credential": "not-a-directory-or-item"})
    assert not daemon.store.get_lane("claude-2")


def test_c9_6_hold_records_an_operator_closure_and_release_lifts_it(core, tmp_path):
    daemon, root = core
    daemon.dispatch("lanes", {"action": "enroll", "credential": str(login_dir(tmp_path, "lane1@example.test"))})
    held = daemon.dispatch("lanes", {"action": "hold", "lane_id": "claude-1", "until": "2027-01-01T00:00:00Z"})
    assert held["held"] == "claude-1"
    closures = daemon.store.query("SELECT * FROM closures WHERE lane_id='claude-1' AND released_at IS NULL")
    assert len(closures) == 1 and closures[0]["reason"] == "operator-hold" and closures[0]["until_at"] == "2027-01-01T00:00:00Z"
    assert closures[0]["scope"] == "account" and closures[0]["clock_source"] == "reported"
    lane_view = next(l for l in held["lanes"] if l["lane_id"] == "claude-1")
    assert lane_view.get("closures") or any(c["lane_id"] == "claude-1" for c in held["closures"])
    released = daemon.dispatch("lanes", {"action": "release", "lane_id": "claude-1"})
    assert released["released"] == "claude-1"
    assert not daemon.store.query("SELECT 1 FROM closures WHERE lane_id='claude-1' AND released_at IS NULL")
    with pytest.raises(protocol.ProtocolError):
        daemon.dispatch("lanes", {"action": "hold", "lane_id": "claude-9", "until": "2027-01-01T00:00:00Z"})
    with pytest.raises(protocol.ProtocolError):
        daemon.dispatch("lanes", {"action": "hold", "lane_id": "claude-1", "until": "soon"})
