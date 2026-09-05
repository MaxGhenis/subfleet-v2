"""`subfleet lanes transfer`: one account moves, both rosters follow.

Plan amendment 8 and `docs/migration.md` principle 1 and shadow-week step 6. The
v1 roster files here are synthetic copies in a temp dir; nothing under
`~/chief-of-staff/subfleet` is opened for writing by any test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subfleet import cli, lanes_transfer, protocol
from subfleet.contracts import Credential, Exit, Lane, LaneOwner
from subfleet.store import Store

CLAUDE_EMAIL = "max@policyengine.org"
OTHER_EMAIL = "mghenis@gmail.com"
CODEX_ACCOUNT = "b3367243-fedb-41e0-84fb-6a66f04f7d00"


@pytest.fixture
def world(tmp_path: Path) -> dict:
    """A v2 state root with two v1-owned lanes, and synthetic v1 roster files."""
    state_root = tmp_path / "v2"
    roster = tmp_path / "v1-roster"
    home = tmp_path / "home"
    state_root.mkdir(parents=True)
    roster.mkdir(parents=True)
    for index in (1, 2):
        (home / f".codex-{index}").mkdir(parents=True)
    (roster / "claude-accounts.json").write_text(json.dumps({
        "_comment": "synthetic",
        "enrolled": {CLAUDE_EMAIL: f"claude-quota-{CLAUDE_EMAIL}",
                     OTHER_EMAIL: f"claude-quota-{OTHER_EMAIL}"},
        "accounts": [CLAUDE_EMAIL, OTHER_EMAIL],
    }, indent=1), encoding="utf-8")
    (roster / "codex-accounts.json").write_text(json.dumps({
        "_comment": "synthetic",
        "protected_account": {"email": "max@maxghenis.com", "account_id": CODEX_ACCOUNT},
        "auto_reset": {"enabled": True},
    }, indent=1), encoding="utf-8")
    store = Store(state_root / "state.sqlite3")
    store.put_lane(Lane("codex-1", "codex", f"codex:{CODEX_ACCOUNT}",
                        Credential("codex", str(home / ".codex-1"), "home"),
                        str(home / ".codex-1"), LaneOwner.V1, False, True))
    store.put_lane(Lane("claude-1", "claude", f"claude:{CLAUDE_EMAIL}",
                        Credential("claude", f"claude-quota-{CLAUDE_EMAIL}", "keychain-token"),
                        None, LaneOwner.V1, False, True))
    return {"store": store, "root": state_root, "roster": roster, "home": home}


def snapshot(paths: list[Path]) -> dict[str, tuple[int, int]]:
    return {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in paths if path.exists()}


def transfer(world: dict, lane: str, to: str, **kwargs) -> dict:
    return lanes_transfer.transfer(world["store"], world["root"], lane, to,
                                   roster_dir=world["roster"], home=world["home"], **kwargs)


def owner(world: dict, lane: str) -> str:
    return world["store"].one("SELECT owner FROM lanes WHERE lane_id=?", (lane,))["owner"]


def roster_json(world: dict, name: str) -> dict:
    return json.loads((world["roster"] / name).read_text())


def lanes_json(world: dict) -> list[dict]:
    payload = json.loads((world["root"] / "lanes.json").read_text())
    return payload["lanes"] if isinstance(payload, dict) else payload


# --- the transfer itself ------------------------------------------------------

def test_transfer_flips_ownership_and_records_an_event(world):
    """C-10.4: ownership changes only by `lanes transfer`, which records an event."""
    result = transfer(world, "codex-1", "v2", confirm_v1_edit=True)
    assert result["applied"] is True and result["from"] == "v1" and result["to"] == "v2"
    assert owner(world, "codex-1") == "v2"
    events = world["store"].query("SELECT * FROM events WHERE kind='lane.transferred'")
    assert len(events) == 1
    data = json.loads(events[0]["data_json"])
    assert data["from"] == "v1" and data["to"] == "v2"
    assert data["account_key"] == f"codex:{CODEX_ACCOUNT}"
    assert events[0]["lane_id"] == "codex-1"


def test_transfer_edits_both_rosters_in_one_step(world):
    """migration.md shadow-week step 6: an event row and both rosters, one step."""
    transfer(world, "claude-1", "v2", confirm_v1_edit=True)
    v1_roster = roster_json(world, "claude-accounts.json")
    assert CLAUDE_EMAIL not in v1_roster["enrolled"]
    assert v1_roster["transferred_to_v2"][CLAUDE_EMAIL] == f"claude-quota-{CLAUDE_EMAIL}"
    assert OTHER_EMAIL in v1_roster["enrolled"]           # only the named account moves
    rows = {row["lane_id"]: row for row in lanes_json(world)}
    assert rows["claude-1"]["owner"] == "v2"


def test_the_v1_edit_is_refused_without_the_flag(world):
    """The one write to a v1 file needs `--i-understand-v1-edit`."""
    before = snapshot([world["roster"] / "claude-accounts.json"])
    with pytest.raises(lanes_transfer.TransferError) as raised:
        transfer(world, "claude-1", "v2")
    assert raised.value.code == Exit.REFUSED
    assert "--i-understand-v1-edit" in (raised.value.fix or "")
    assert snapshot([world["roster"] / "claude-accounts.json"]) == before
    assert owner(world, "claude-1") == "v1"               # and nothing else moved
    assert not (world["root"] / "lanes.json").exists()


def test_a_backup_is_written_beside_the_v1_file(world):
    """The v1 edit copies the file first, beside itself."""
    original = (world["roster"] / "claude-accounts.json").read_text()
    result = transfer(world, "claude-1", "v2", confirm_v1_edit=True)
    backups = sorted(world["roster"].glob("claude-accounts.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    assert str(backups[0]) in json.dumps(result)


def test_dry_run_prints_the_diff_and_writes_nothing(world):
    """`--dry-run` never acts (C-19.1)."""
    watched = [world["roster"] / "claude-accounts.json", world["root"] / "lanes.json"]
    before = snapshot(watched)
    result = transfer(world, "claude-1", "v2", dry_run=True, confirm_v1_edit=True)
    assert result["dry_run"] is True and result["applied"] is False
    assert result["changed"] is True
    assert "-  \"max@policyengine.org\"" in result["diff"] or "enrolled" in result["diff"]
    assert "transferred_to_v2" in result["diff"]
    assert snapshot(watched) == before
    assert owner(world, "claude-1") == "v1"
    assert not world["store"].query("SELECT * FROM events WHERE kind='lane.transferred'")
    assert not sorted(world["roster"].glob("*.bak-*"))


def test_dry_run_is_refused_by_nothing_and_needs_no_flag(world):
    """A dry run shows the v1 diff without the flag: it writes nothing."""
    result = transfer(world, "claude-1", "v2", dry_run=True)
    assert result["changed"] is True and result["applied"] is False


def test_transferring_back_restores_the_v1_roster(world):
    """Rollback transfers accounts back one at a time (plan amendment 8)."""
    transfer(world, "claude-1", "v2", confirm_v1_edit=True)
    transfer(world, "claude-1", "v1", confirm_v1_edit=True)
    v1_roster = roster_json(world, "claude-accounts.json")
    assert v1_roster["enrolled"][CLAUDE_EMAIL] == f"claude-quota-{CLAUDE_EMAIL}"
    assert "transferred_to_v2" not in v1_roster
    assert owner(world, "claude-1") == "v1"
    assert lanes_json(world)[0]["owner"] == "v1"
    assert len(world["store"].query("SELECT * FROM events WHERE kind='lane.transferred'")) == 2


def test_a_second_transfer_to_the_same_owner_changes_nothing(world):
    transfer(world, "codex-1", "v2", confirm_v1_edit=True)
    watched = [world["roster"] / "codex-accounts.json", world["root"] / "lanes.json"]
    before = snapshot(watched)
    result = transfer(world, "codex-1", "v2", confirm_v1_edit=True)
    assert result["changed"] is False
    assert snapshot(watched) == before
    assert len(world["store"].query("SELECT * FROM events WHERE kind='lane.transferred'")) == 1


def test_a_codex_transfer_names_the_follow_up_that_enforces_it(world):
    """v1 globs `~/.codex-1..9` (paths.codex_homes); the file alone is a record."""
    result = transfer(world, "codex-1", "v2", confirm_v1_edit=True)
    recorded = roster_json(world, "codex-accounts.json")["transferred_to_v2"]
    assert [row["home"] for row in recorded] == [str(world["home"] / ".codex-1")]
    assert recorded[0]["account_id"] == CODEX_ACCOUNT
    follow_up = " ".join(result["follow_up"])
    assert "SUBFLEET_CODEX_HOMES=" + str(world["home"] / ".codex-2") in follow_up
    assert str(world["home"] / ".codex-1") not in follow_up.split("SUBFLEET_CODEX_HOMES=")[1].split()[0]


def test_an_unknown_lane_is_exit_two(world):
    with pytest.raises(lanes_transfer.TransferError) as raised:
        transfer(world, "codex-9", "v2", confirm_v1_edit=True)
    assert raised.value.code == Exit.INVALID_INPUT


def test_an_owner_other_than_v1_or_v2_is_refused(world):
    with pytest.raises(lanes_transfer.TransferError):
        transfer(world, "codex-1", "v3", confirm_v1_edit=True)


def test_a_missing_v1_roster_is_never_created(world):
    """This repo edits a v1 file it has read; it never writes one from nothing."""
    (world["roster"] / "claude-accounts.json").unlink()
    with pytest.raises(lanes_transfer.TransferError) as raised:
        transfer(world, "claude-1", "v2", confirm_v1_edit=True)
    assert raised.value.code == Exit.OPERATIONAL
    assert not (world["roster"] / "claude-accounts.json").exists()
    assert owner(world, "claude-1") == "v1"


def test_v1_stops_before_v2_starts(world, monkeypatch):
    """principle 5: never two schedulers on one account, even mid-failure."""
    published: list[str] = []
    real = lanes_transfer._publish

    def publish(path: Path, text: str) -> None:
        published.append(str(path))
        if str(path).endswith("lanes.json"):
            raise OSError("disk full")
        real(path, text)

    monkeypatch.setattr(lanes_transfer, "_publish", publish)
    with pytest.raises(OSError):
        transfer(world, "claude-1", "v2", confirm_v1_edit=True)
    assert published[0].endswith("claude-accounts.json")     # v1 dropped it first
    assert CLAUDE_EMAIL not in roster_json(world, "claude-accounts.json")["enrolled"]
    assert owner(world, "claude-1") == "v2"                  # nobody is left dispatching


# --- the CLI and the daemon op ------------------------------------------------

def test_the_cli_sends_the_transfer_arguments(root, daemon, capsys):
    """C-17.1: `subfleet lanes transfer <lane> --to v1|v2`; the CLI stays thin."""
    server = daemon({"lanes": lambda request: {
        "transfer": {"lane_id": "codex-1", "from": "v1", "to": "v2", "changed": True,
                     "dry_run": False, "applied": True, "diff": "",
                     "edits": [{"path": "/tmp/lanes.json", "owner": "v2", "changed": True,
                                "backup": None}],
                     "follow_up": ["set SUBFLEET_CODEX_HOMES"]},
        "lanes": []}})
    code = cli.main(["lanes", "transfer", "codex-1", "--to", "v2", "--i-understand-v1-edit"])
    assert code == int(Exit.OK)
    args = server.args("lanes")
    assert args["action"] == "transfer" and args["lane_id"] == "codex-1"
    assert args["owner"] == "v2" and args["confirm_v1_edit"] is True
    assert args["dry_run"] is False
    printed = capsys.readouterr().out
    assert "codex-1: v1 -> v2" in printed
    assert "next: set SUBFLEET_CODEX_HOMES" in printed


def test_the_cli_dry_run_flag_reaches_the_daemon(root, daemon, capsys):
    server = daemon({"lanes": lambda request: {
        "transfer": {"lane_id": "claude-1", "from": "v1", "to": "v2", "changed": True,
                     "dry_run": True, "applied": False,
                     "diff": "--- a/claude-accounts.json\\n+++ b/claude-accounts.json\\n",
                     "edits": [], "follow_up": []},
        "lanes": []}})
    assert cli.main(["lanes", "transfer", "claude-1", "--to", "v2", "--dry-run"]) == int(Exit.OK)
    assert server.args("lanes")["dry_run"] is True
    assert server.args("lanes")["confirm_v1_edit"] is False
    assert "dry run, nothing written" in capsys.readouterr().out


def test_the_cli_refuses_an_owner_that_is_not_v1_or_v2(root, capsys):
    """C-17.3: a bad argument is exit 2, without reaching the daemon."""
    assert cli.main(["lanes", "transfer", "codex-1", "--to", "v3"]) == int(Exit.INVALID_INPUT)
    assert "invalid choice" in capsys.readouterr().err


def test_the_daemon_op_carries_the_refusal_code(world):
    """The daemon's `lanes` op reports the refusal as the CLI's exit code (C-16.1)."""
    args = protocol.LanesArgs(action="transfer", lane_id="claude-1", owner="v2")
    coerced = protocol.coerce_args(protocol.LanesArgs, {"action": args.action,
                                                        "lane_id": args.lane_id,
                                                        "owner": args.owner})
    with pytest.raises(lanes_transfer.TransferError) as raised:
        lanes_transfer.transfer(world["store"], world["root"], coerced.lane_id, coerced.owner,
                                dry_run=coerced.dry_run,
                                confirm_v1_edit=coerced.confirm_v1_edit,
                                roster_dir=world["roster"], home=world["home"])
    assert int(raised.value.code) == int(Exit.REFUSED)
