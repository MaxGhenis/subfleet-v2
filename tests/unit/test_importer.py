"""The v1 import, row by row of `docs/migration.md`.

The fixture builds a synthetic v1 state tree in a temp dir from the formats read
off the real v1 state on 2026-09-05 (recorded in `PROGRESS.md`), so no test here
touches `~/chief-of-staff/state/subfleet`. The one pass over the real thing is
`tests/live/test_import_dry_run.py`, gated by `SUBFLEET_LIVE=1` (C-20.1).
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from subfleet import importer
from subfleet.conversations import legacy
from subfleet.conversations.store import ConversationStore
from subfleet.importer import ImportRefused, import_legacy_cockpit, import_v1
from subfleet.store import Store
from tests.legacy_fixtures import (
    LEGACY, LEGACY_SESSION, QUEUED_SESSION, outbox_row, write_outbox, write_transcript,
)

DESKTOP = "max@rulesatlas.org"
ENROLLED = "max@policyengine.org"
SECOND = "mghenis@gmail.com"
CODEX_ONE = "b3367243-fedb-41e0-84fb-6a66f04f7d00"
CODEX_TWO = "3163881b-4300-4ed1-a2ec-9c5360d10db8"
APP_ACCOUNT = "390a6216-cdbe-478e-b9f1-42a9dc7f5dd7"
SESSION = "29c03102-0afc-452f-a605-14a356d334bc"

def _dead_pid() -> int:
    """A pid `ps` accepts and no process holds: a child that has already been reaped."""
    child = subprocess.Popen(["/usr/bin/true"])
    child.wait()
    return child.pid


DEAD_PID = _dead_pid()


def offset_now(delta_s: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=delta_s)
    return moment.astimezone().isoformat(timespec="seconds")


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def run_meta(run_id: str, **overrides: object) -> dict:
    meta = {
        "id": run_id, "family": "codex", "model": "gpt-6-astra",
        "lane": "/HOME/.codex-1", "workdir": "/tmp/work",
        "git_head_before": "a" * 40, "git_head_after": "a" * 40, "rc": 0,
        "started_at": offset_now(-600), "finished_at": offset_now(-60),
        "duration_s": 540.0, "original_out_path": None, "out_path": None,
        "session_id": None, "transcript_path": None,
        "codex_thread_id": "01a07281-5833-72f1-b299-1537f2ae87d0",
        "codex_home": "/HOME/.codex-1", "rollout_path": None, "resumed_from": None,
        "salvage_refs": [], "caller": {"session_id": SESSION, "pid": 20851},
        "pid": DEAD_PID, "launcher": "subfleet run", "notify": None,
        "routing_decision": {"class": "build", "task": "build", "tier": "standard",
                             "requested_model": "opus", "model": "astra",
                             "overrides": {"task": "build", "tier": "standard",
                                           "sandbox": "workspace-write"}},
    }
    meta.update(overrides)
    return meta


@pytest.fixture
def v1(tmp_path: Path) -> dict:
    """A synthetic v1 state tree: roster, homes, runs, notices, outbox, files."""
    home = tmp_path / "home"
    state = tmp_path / "v1-state"
    delegate = tmp_path / "delegate"
    roster = tmp_path / "v1-roster"
    for directory in (home, state, delegate, roster):
        directory.mkdir(parents=True, exist_ok=True)

    write_json(home / ".claude.json", {"oauthAccount": {"emailAddress": DESKTOP,
                                                        "accountUuid": "aa53aefb"}})
    write_json(roster / "claude-accounts.json", {
        "_comment": "synthetic",
        "enrolled": {ENROLLED: f"claude-quota-{ENROLLED}",
                     SECOND: f"claude-quota-{SECOND}"},
        "accounts": [ENROLLED, SECOND, DESKTOP],
    })
    write_json(roster / "codex-accounts.json", {
        "_comment": "synthetic",
        "protected_account": {"email": "max@maxghenis.com", "account_id": CODEX_ONE},
        "auto_reset": {"enabled": True, "headroom_floor_pct": 15, "min_interval_min": 30},
    })
    for name, account in ((".codex-1", CODEX_ONE), (".codex-2", CODEX_TWO),
                          (".codex", APP_ACCOUNT)):
        write_json(home / name / "auth.json", {
            "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"id_token": "x" * 40, "access_token": "y" * 40,
                       "refresh_token": "z" * 40, "account_id": account},
            "last_refresh": offset_now(-3600)})
    write_json(home / ".codex-9" / "auth.json", {
        "auth_mode": "apikey", "OPENAI_API_KEY": "sk-test", "tokens": {}})

    home_text = str(home)

    def rewrite(value: object) -> object:
        if isinstance(value, str):
            return value.replace("/HOME", home_text)
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    runs = state / "runs"
    metas = {
        "20260905-100000-ok": run_meta("20260905-100000-ok", rc=0),
        "20260905-100100-limited": run_meta("20260905-100100-limited", rc=4),
        "20260905-100200-killed": run_meta("20260905-100200-killed", rc=-9),
        "20260905-100300-lost": run_meta("20260905-100300-lost", rc=None,
                                         finished_at=None, duration_s=None),
        "20260905-100400-live": run_meta("20260905-100400-live", rc=None,
                                         finished_at=None, duration_s=None,
                                         pid=os.getpid()),
        "20260905-100500-claude": run_meta("20260905-100500-claude", family="claude",
                                           model="claude-opus-5", lane=ENROLLED,
                                           codex_home=None, codex_thread_id=None,
                                           session_id="409a2828-3b59-497a-9602-6bf12e80670c",
                                           rc=0),
        "20260905-100600-nolane": run_meta("20260905-100600-nolane", lane="/HOME/.codex-8",
                                           codex_home="/HOME/.codex-8", rc=0),
    }
    for run_id, meta in metas.items():
        directory = runs / run_id
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "meta.json", rewrite(meta))
        (directory / "prompt.md").write_text(f"prompt for {run_id}\n", encoding="utf-8")
        if meta["rc"] is not None:
            (directory / "out.md").write_text(f"deliverable {run_id}\n", encoding="utf-8")
            (directory / "err.log").write_text("stderr\n", encoding="utf-8")
            (directory / "lane.log").write_text("lane\n", encoding="utf-8")

    notices = state / "notices"
    notices.mkdir(parents=True, exist_ok=True)
    (notices / f"{SESSION}.jsonl").write_text("\n".join(json.dumps(entry) for entry in [
        {"run_id": "20260905-100000-ok", "rc": 0, "surfaced": True,
         "surfaced_at": offset_now(-30), "text": "run finished", "ts": offset_now(-40),
         "push": {"delivered": True}},
        {"run_id": "20260905-100100-limited", "rc": 4, "surfaced": False,
         "text": "run limited", "ts": offset_now(-20), "push": {"delivered": False}},
        {"run_id": "20260101-000000-forgotten", "rc": 0, "surfaced": False,
         "text": "older than the ledger", "ts": offset_now(-10)},
        {"run_id": "20260905-101000-late", "rc": 0, "surfaced": False,
         "text": "its run reaches the ledger on a later pass", "ts": offset_now(-5)},
    ]) + "\n", encoding="utf-8")

    # The legacy cockpit (C-30.4): two finished messages in a session whose
    # transcript exists, and one still queued in another session.
    claude = tmp_path / "claude"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_transcript(claude, LEGACY_SESSION, workspace)
    write_transcript(claude, QUEUED_SESSION, workspace)
    write_outbox(state, [
        outbox_row(LEGACY[0], LEGACY_SESSION, "finished", "he replied", at=300),
        outbox_row(LEGACY[1], LEGACY_SESSION, "finished", "and a follow-up", at=200),
        outbox_row(LEGACY[2], QUEUED_SESSION, "queued", "still queued", at=100),
    ])

    write_json(state / "capacity-live-cache.json", {
        "probed_at": offset_now(-30),
        "accounts": [
            {"family": "codex", "id": f"{home}/.codex-1", "email": "max@maxghenis.com",
             "account_id": CODEX_ONE,
             "five_hour": {"used_percent": None, "reset_at": None, "confidence": None},
             "weekly": {"used_percent": 4, "reset_at": offset_now(600_000),
                        "confidence": "live"},
             "learned_capacity": None, "scoped_limits": []},
            {"family": "codex", "id": f"{home}/.codex-2", "email": SECOND,
             "account_id": CODEX_TWO,
             "five_hour": {"used_percent": 88, "reset_at": offset_now(3600),
                           "confidence": "learned"},
             "weekly": {"used_percent": 97, "reset_at": offset_now(200_000),
                        "confidence": "live"},
             "learned_capacity": 1_000_000, "scoped_limits": []},
        ],
    })
    write_json(state / "claude-oauth-raw.json", {
        "checked_at": offset_now(-20),
        "raw": {
            "five_hour": {"utilization": 53.0, "resets_at": offset_now(20_000)},
            "seven_day": {"utilization": 46.0, "resets_at": offset_now(400_000)},
            "seven_day_opus": None,
            "nimbus_quill": {"utilization": 0.0, "resets_at": None},
            "limits": [
                {"kind": "session", "percent": 53, "resets_at": offset_now(20_000),
                 "scope": None, "is_active": True},
                {"kind": "weekly_scoped", "percent": 8, "resets_at": offset_now(400_000),
                 "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                 "is_active": False},
            ],
        },
    })
    write_json(state / "keepalive.json", {
        "schema_version": 1, "updated_at": offset_now(-60),
        "last_run": {"family": "claude", "opened": 1},
        "lanes": {
            ENROLLED: {"last_outcome": "opened", "last_checked_at": offset_now(-60),
                       "last_attempt_at": offset_now(-120), "last_opened_at": offset_now(-120)},
            SECOND: {"last_outcome": "skipped-auth", "last_checked_at": offset_now(-60),
                     "last_attempt_at": offset_now(-120), "auth_code": 401},
        },
    })
    write_json(state / "reset-policy.json", {
        "last_redeemed_at": offset_now(-4000), "lane": f"{home}/.codex-1",
        "email": "max@maxghenis.com",
        "credit_id": "RateLimitResetCredit_26c531af84688191afcbab1b15c7ec69",
        "last_redemptions": {f"{home}/.codex-1": offset_now(-4000),
                             f"{home}/.codex-2": offset_now(-90_000)},
    })
    write_json(delegate / "cooldowns.json", {
        f"{home}/.codex-2": {"*": offset_now(7200)},
        ENROLLED: {"claude-opus-5": offset_now(3600), "sonnet": offset_now(-3600)},
        "max@not-in-the-roster.example": {"*": offset_now(3600)},
    })
    write_json(state / "alerts.json", {
        "claude-limit:2026-07-11T18:40:00-04:00": {"active": False,
                                                   "last_sent": offset_now(-100_000)},
        f"codex-revoked:{home}/.codex-2": {"active": True, "last_sent": offset_now(-500)},
    })
    write_json(state / "native-workers.json", {
        "claude:0957da09-b6d9-4911-b2f6-407ec5ca20be": {
            "pid": 77701, "broker_pid": 50056, "account": ENROLLED}})
    write_json(state / "tickles" / f"{SESSION}.json", {
        "session_id": SESSION, "at": offset_now(-800), "last_uuid": "3388240e",
        "turn_uuid": "3388240e", "restart_stubs": 0, "delivered": True})
    (state / "integration-events.salt").write_bytes(b"0123456789abcdef0123456789abcdef")

    # A store the manifest does not name, and one it drops.
    write_json(state / "mystery-store.json", {"unknown": True})
    write_json(state / "snapshot.json", {"derived": True})

    return {"home": home, "state": state, "delegate": delegate, "roster": roster,
            "root": tmp_path / "v2", "runs": runs, "metas": metas,
            "claude": claude, "workspace": workspace}


def run_import(v1: dict, **kwargs):
    return import_v1(v1["root"], v1_state=v1["state"], delegate_state=v1["delegate"],
                     roster_dir=v1["roster"], home=v1["home"], **kwargs)


def dump(root: Path) -> dict[str, list[tuple]]:
    """Every row of every table but `events`, which is append-only by design."""
    connection = sqlite3.connect(root / "state.sqlite3")
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    snapshot = {}
    for table in sorted(tables):
        if table in ("events", "schema_version"):
            continue
        snapshot[table] = sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall())
    connection.close()
    return snapshot


def rows(root: Path, sql: str, params: tuple = ()) -> list[dict]:
    with Store(root / "state.sqlite3", read_only=True) as store:
        return store.query(sql, params)


# --- idempotence and incrementality ------------------------------------------

def test_a_second_pass_changes_nothing(v1):
    """migration.md principle 4: re-running the importer changes nothing already imported."""
    first = run_import(v1)
    before = dump(v1["root"])
    second = run_import(v1)
    assert dump(v1["root"]) == before
    assert first.imported > 0
    assert second.imported == 0, second.as_dict()["stores"]


def test_every_store_keeps_a_cursor_in_events(v1):
    """migration.md principle 4: a cursor per store (last id, last mtime, last line)."""
    run_import(v1)
    cursors = {json.loads(row["data_json"])["store"]: json.loads(row["data_json"])["cursor"]
               for row in rows(v1["root"], "SELECT data_json FROM events WHERE kind='import.cursor'")}
    assert {"runs", "notices", "capacity-live-cache", "claude-oauth-raw",
            "keepalive", "reset-policy", "cooldowns"} <= set(cursors)
    assert cursors["runs"]["last_id"] == "20260905-100600-nolane"
    assert cursors["notices"]["files"][f"{SESSION}.jsonl"]["lines"] == 4
    # The two entries whose runs are not in the ledger are kept for retry.
    assert cursors["notices"]["files"][f"{SESSION}.jsonl"]["retry"] == [2, 3]
    assert "outbox" not in cursors          # staged until milestone 9 (C-30.4)


def test_a_new_run_imports_incrementally(v1):
    """The cursor reads only what is new on a later pass."""
    run_import(v1)
    later = "20260905-110000-later"
    directory = v1["runs"] / later
    directory.mkdir()
    meta = run_meta(later, rc=0, lane=str(v1["home"] / ".codex-1"),
                    codex_home=str(v1["home"] / ".codex-1"))
    write_json(directory / "meta.json", meta)
    (directory / "prompt.md").write_text("later\n", encoding="utf-8")
    (directory / "out.md").write_text("later out\n", encoding="utf-8")
    report = run_import(v1)
    # The new run, plus everything the first pass left open: the two runs v1 has
    # not given an rc, and the one whose lane is not in the roster (principles 3, 4).
    assert report.stores["runs"].seen == 4
    assert report.stores["runs"].imported == 1
    assert report.stores["runs"].reasons["already-imported"] == 2
    assert report.stores["runs"].reasons["no-lane-for-run"] == 1
    assert rows(v1["root"], "SELECT job_id FROM jobs WHERE job_id=?", (later,))


# --- the roster row -----------------------------------------------------------

def test_roster_accounts_become_lanes_owned_by_v1(v1):
    """Manifest row `claude-accounts.json, codex-accounts.json`: owner v1 (C-10.4)."""
    run_import(v1)
    lanes = {row["lane_id"]: row for row in rows(v1["root"], "SELECT * FROM lanes")}
    assert all(row["owner"] == "v1" for row in lanes.values())
    assert {row["account_key"] for row in lanes.values()} >= {
        f"claude:{ENROLLED}", f"claude:{SECOND}", f"claude:{DESKTOP}",
        f"codex:{CODEX_ONE}", f"codex:{CODEX_TWO}"}


@pytest.mark.parametrize("profile_matches", [True, False])
def test_imported_claude_label_allows_profile_comparison_without_claiming_identity(v1, profile_matches):
    from subfleet.adapters.claude import ClaudeAdapter
    from tests.fake.profile import usage_body

    run_import(v1)
    row = rows(v1["root"], "SELECT * FROM lanes WHERE account_key=?", (f"claude:{ENROLLED}",))[0]
    assert row["label"] == ENROLLED and row["identity"] is None
    assert row["identity_status"] == "unverified"
    calls = []

    def profile(request, timeout):
        calls.append("profile")
        return 200, json.dumps({"account": {"email": ENROLLED if profile_matches else SECOND,
                                            "uuid": "fixture-account"},
                                "organization": {"uuid": "fixture-org"}}).encode()

    def usage(request, timeout):
        calls.append("usage")
        return 200, usage_body(.25)

    adapter = ClaudeAdapter(profile_opener=profile, usage_opener=usage)
    with Store(v1["root"] / "state.sqlite3") as store:
        result = adapter.probe_usage(store.get_lane(row["lane_id"]), {"CLAUDE_CODE_OAUTH_TOKEN": "fake-only"})
    assert result.status == ("ok" if profile_matches else "identity-unbound")
    assert calls == (["profile", "usage"] if profile_matches else ["profile"])
    assert rows(v1["root"], "SELECT * FROM lanes WHERE lane_id=?", (row["lane_id"],))[0] == row


@pytest.mark.parametrize("identity_status", [None, "mismatch", "verified"])
def test_incremental_import_repairs_missing_claude_label_after_transfer_only_once(v1, identity_status):
    from subfleet.lanes_transfer import transfer

    roster_path = v1["roster"] / "claude-accounts.json"
    roster = json.loads(roster_path.read_text())
    roster["enrolled"][ENROLLED] = "custom-imported-reference"
    write_json(roster_path, roster)
    run_import(v1)
    with Store(v1["root"] / "state.sqlite3") as store:
        row = store.one("SELECT * FROM lanes WHERE account_key=?", (f"claude:{ENROLLED}",))
        transfer(store, v1["root"], row["lane_id"], "v2", roster_dir=v1["roster"],
                 home=v1["home"], v1_state=v1["state"], confirm_v1_edit=True)
        # Recreate the omitted fields of an earlier import without overwriting
        # a later mismatch/verified profile finding or changing account ownership.
        with store.transaction("test.old-import") as conn:
            conn.execute("UPDATE lanes SET label=NULL,identity_status=?,enabled=0 WHERE lane_id=?",
                         (identity_status, row["lane_id"]))
        before = store.one("SELECT * FROM lanes WHERE lane_id=?", (row["lane_id"],))
        before_actions = store.query("SELECT * FROM actions ORDER BY action_id")
    report = run_import(v1)
    after = rows(v1["root"], "SELECT * FROM lanes WHERE lane_id=?", (row["lane_id"],))[0]
    expected = {**before, "label": ENROLLED, "updated_at": after["updated_at"],
                "identity_status": identity_status or "unverified"}
    assert after == expected and after["owner"] == "v2" and after["enabled"] == 0
    assert report.stores["roster"].reasons["missing-operator-label-repaired"] == 1
    assert len(rows(v1["root"], "SELECT * FROM lanes WHERE account_key=?", (f"claude:{ENROLLED}",))) == 1
    assert rows(v1["root"], "SELECT * FROM actions ORDER BY action_id") == before_actions
    again = run_import(v1)
    assert again.stores["roster"].reasons.get("missing-operator-label-repaired", 0) == 0
    assert rows(v1["root"], "SELECT * FROM lanes WHERE lane_id=?", (row["lane_id"],))[0] == after


def test_reimport_preserves_recorded_claude_label_and_identity(v1):
    run_import(v1)
    with Store(v1["root"] / "state.sqlite3") as store:
        row = store.one("SELECT * FROM lanes WHERE account_key=?", (f"claude:{ENROLLED}",))
        store.update_lane(row["lane_id"], label="operator-recorded@example.test",
                          identity="account-uuid:org-uuid", identity_status="verified")
        before = store.one("SELECT * FROM lanes WHERE lane_id=?", (row["lane_id"],))
    run_import(v1)
    assert rows(v1["root"], "SELECT * FROM lanes WHERE lane_id=?", (row["lane_id"],))[0] == before


def test_credential_refs_are_copied_never_values(v1):
    """Manifest row: "credential refs copied, never values" (C-10.1, C-10.5)."""
    run_import(v1)
    for row in rows(v1["root"], "SELECT * FROM lanes"):
        if row["provider"] == "claude":
            assert row["credential_ref"].startswith("claude-quota-")
            assert row["credential_kind"] == "keychain-token"
        else:
            assert row["credential_kind"] == "home"
            auth = json.loads(Path(row["credential_ref"], "auth.json").read_text())
            assert auth["tokens"]["access_token"] not in json.dumps(row)


def test_desktop_comes_from_claude_json_and_codex_app_home_is_not_a_lane(v1):
    """Manifest row: "`desktop` set from `~/.claude.json` `oauthAccount`" (C-10.3)."""
    run_import(v1)
    desktop = rows(v1["root"], "SELECT * FROM lanes WHERE desktop=1")
    assert [row["account_key"] for row in desktop] == [f"claude:{DESKTOP}"]
    homes = {row["home"] for row in rows(v1["root"], "SELECT * FROM lanes WHERE provider='codex'")}
    assert str(v1["home"] / ".codex") not in homes
    assert homes == {str(v1["home"] / ".codex-1"), str(v1["home"] / ".codex-2")}


def test_codex_account_key_is_read_from_auth_json(v1):
    """Manifest row: "each Codex home's account key read from its `auth.json`" (C-1.4)."""
    run_import(v1)
    lane = rows(v1["root"], "SELECT * FROM lanes WHERE home=?",
                (str(v1["home"] / ".codex-1"),))[0]
    assert lane["account_key"] == f"codex:{CODEX_ONE}"
    assert lane["lane_id"] == "codex-1"


def test_an_api_key_home_is_refused(v1):
    """C-10.2: enrolment refuses an API-key login."""
    report = run_import(v1)
    assert report.stores["roster"].reasons.get("api-key-login-refused") == 1
    assert not rows(v1["root"], "SELECT * FROM lanes WHERE home LIKE '%.codex-9'")


def test_a_lane_id_survives_the_roster_growing(v1):
    """C-1.3: lane ids are stable; a new v1 account never renames an existing lane."""
    run_import(v1)
    before = {row["account_key"]: row["lane_id"]
              for row in rows(v1["root"], "SELECT * FROM lanes")}
    roster = json.loads((v1["roster"] / "claude-accounts.json").read_text())
    roster["accounts"].insert(0, "max@new-account.example")     # v1 prepends an account
    roster["enrolled"]["max@new-account.example"] = "claude-quota-max@new-account.example"
    write_json(v1["roster"] / "claude-accounts.json", roster)
    run_import(v1)
    after = {row["account_key"]: row["lane_id"]
             for row in rows(v1["root"], "SELECT * FROM lanes")}
    assert {key: value for key, value in after.items() if key in before} == before
    assert after["claude:max@new-account.example"] not in before.values()


def test_reimport_never_takes_an_account_back_from_v2(v1):
    """C-10.4, principle 1: ownership changes only by `lanes transfer`."""
    run_import(v1)
    with Store(v1["root"] / "state.sqlite3") as store:
        store.update_lane("codex-1", owner="v2")
    run_import(v1)
    assert rows(v1["root"], "SELECT owner FROM lanes WHERE lane_id='codex-1'")[0]["owner"] == "v2"


def test_reimport_after_codex_move_keeps_reset_history_and_new_runs_on_original_account(v1):
    from subfleet.lanes_transfer import transfer

    run_import(v1)
    for path in (v1["state"] / "runs").glob("*/meta.json"):
        meta = json.loads(path.read_text())
        if meta.get("rc") is None:
            meta.update(rc=0, finished_at=offset_now(-5))
            write_json(path, meta)
    before_actions = rows(v1["root"], "SELECT * FROM actions ORDER BY action_id")
    with Store(v1["root"] / "state.sqlite3") as store:
        transfer(store, v1["root"], "codex-1", "v2", roster_dir=v1["roster"],
                 home=v1["home"], v1_state=v1["state"], agents_dir=v1["root"] / "no-agents",
                 confirm_v1_edit=True)
        lane_before = store.one("SELECT * FROM lanes WHERE lane_id='codex-1'")
    run_id = "20260905-101200-transferred"
    write_json(v1["state"] / "runs" / run_id / "meta.json",
               run_meta(run_id, lane=str(v1["home"] / ".codex-1"),
                        codex_home=str(v1["home"] / ".codex-1")))
    cache = json.loads((v1["state"] / "capacity-live-cache.json").read_text())
    cache["probed_at"] = offset_now(-1)
    write_json(v1["state"] / "capacity-live-cache.json", cache)
    run_import(v1)
    assert rows(v1["root"], "SELECT * FROM actions ORDER BY action_id") == before_actions
    assert rows(v1["root"], "SELECT lane_id FROM attempts WHERE job_id=?", (run_id,)) == [{"lane_id": "codex-1"}]
    assert rows(v1["root"], "SELECT * FROM lanes WHERE lane_id='codex-1'")[0] == lane_before
    assert len(rows(v1["root"], "SELECT * FROM readings WHERE lane_id='codex-1' AND source='wham'")) == 2
    run_import(v1)
    assert rows(v1["root"], "SELECT * FROM actions ORDER BY action_id") == before_actions


@pytest.mark.parametrize("bad_evidence", ["absent", "wrong-account", "wrong-destination", "wrong-direction"])
def test_old_codex_home_alias_requires_matching_transfer_evidence(v1, bad_evidence):
    run_import(v1)
    old = str(v1["home"] / ".codex-1")
    parked = str(v1["root"] / "lanes" / "codex-1")
    with Store(v1["root"] / "state.sqlite3") as store:
        with store.transaction("test.relocated") as conn:
            conn.execute("UPDATE lanes SET home=?,credential_ref=?,owner='v2' WHERE lane_id='codex-1'", (parked, parked))
        data = {"from": "v1", "to": "v2", "account_key": f"codex:{CODEX_ONE}", "home_move": [old, parked]}
        if bad_evidence == "wrong-account":
            data["account_key"] = f"codex:{CODEX_TWO}"
        elif bad_evidence == "wrong-destination":
            data["home_move"][1] = parked + "-other"
        elif bad_evidence == "wrong-direction":
            data["from"], data["to"] = "v2", "v1"
        if bad_evidence != "absent":
            store.add_event("lane.transferred", lane_id="codex-1", data=data)
        index = importer._lane_index(importer._Writer(store, False), v1["home"])
        assert importer._lane_of(index, old, v1["home"]) is None
        assert importer._lane_of(index, "~/.codex-1", v1["home"]) is None


@pytest.mark.parametrize("current_binding", [False, True])
def test_reused_codex_home_cannot_choose_between_historical_account_bindings(v1, current_binding):
    from subfleet.contracts import Credential, Lane, LaneOwner

    run_import(v1)
    old = str(v1["home"] / ".codex-1")
    with Store(v1["root"] / "state.sqlite3") as store:
        for lane_id, account in (("codex-1", CODEX_ONE), ("codex-2", CODEX_TWO)):
            parked = str(v1["root"] / "lanes" / lane_id)
            with store.transaction("test.relocated") as conn:
                conn.execute("UPDATE lanes SET home=?,credential_ref=?,owner='v2' WHERE lane_id=?",
                             (parked, parked, lane_id))
            store.add_event("lane.transferred", lane_id=lane_id,
                            data={"from": "v1", "to": "v2", "account_key": f"codex:{account}",
                                  "home_move": [old, parked]})
        if current_binding:
            store.put_lane(Lane("current", "codex", "codex:current-account",
                                Credential("codex", old, "home"), old, LaneOwner.V1, False))
        index = importer._lane_index(importer._Writer(store, False), v1["home"])
        expected = "current" if current_binding else None
        assert importer._lane_of(index, old, v1["home"]) == expected
        assert importer._lane_of(index, "~/.codex-1", v1["home"]) == expected
        if not current_binding:
            before_actions = store.query("SELECT * FROM actions ORDER BY action_id")
            report = importer.StoreReport("reset-policy", "import", "actions")
            importer.import_reset_policy(importer._Writer(store, False), report,
                                         v1_state=v1["state"], home=v1["home"], cursor={})
            assert report.reasons["reset-home-without-account-binding"] >= 1
            assert store.query("SELECT * FROM actions ORDER BY action_id") == before_actions


# --- the runs row -------------------------------------------------------------

def test_run_ids_and_request_ids(v1):
    """Manifest row `S/runs/`: "`job_id` keeps the v1 id; `request_id` = `v1:<id>`"."""
    run_import(v1)
    job = rows(v1["root"], "SELECT * FROM jobs WHERE job_id='20260905-100000-ok'")[0]
    assert job["request_id"] == "v1:20260905-100000-ok"
    assert job["kind"] == "dispatch"
    assert rows(v1["root"], "SELECT * FROM attempts WHERE attempt_id=?",
                ("20260905-100000-ok/a1",))                      # C-1.2


def test_states_map_from_rc(v1):
    """Manifest row `S/runs/`: 0 succeeded, 4 or 5 failed, -9/143 interrupted,
    never finalized lost."""
    run_import(v1)
    states = {row["job_id"]: row["state"] for row in rows(v1["root"], "SELECT * FROM jobs")}
    assert states["20260905-100000-ok"] == "succeeded"
    assert states["20260905-100100-limited"] == "failed"
    # C-4.1 has no `interrupted` job state; the attempt keeps it (C-4.2).
    assert states["20260905-100200-killed"] == "failed"
    assert states["20260905-100300-lost"] == "lost"
    attempt = rows(v1["root"], "SELECT * FROM attempts WHERE job_id='20260905-100200-killed'")[0]
    assert attempt["signal"] == 9 and attempt["state"] == "interrupted"


def test_artifacts_point_at_the_v1_paths_and_are_not_copied(v1):
    """Manifest row `S/runs/`: "`artifacts` rows point at the v1 paths (not copied)"."""
    run_import(v1)
    artifacts = rows(v1["root"], "SELECT * FROM artifacts WHERE attempt_id=?",
                     ("20260905-100000-ok/a1",))
    by_role = {row["role"]: row for row in artifacts}
    assert set(by_role) == {"deliverable", "stderr", "lane-log"}
    deliverable = Path(by_role["deliverable"]["path"])
    assert deliverable == v1["runs"] / "20260905-100000-ok" / "out.md"
    assert deliverable.read_text() == "deliverable 20260905-100000-ok\n"
    assert not (v1["root"] / "jobs" / "20260905-100000-ok" / "out.md").exists()


def test_a_live_run_is_external_and_never_adopted(v1):
    """migration.md principle 3: live at import time is `running`, `imported_external`."""
    run_import(v1)
    job = rows(v1["root"], "SELECT * FROM jobs WHERE job_id='20260905-100400-live'")[0]
    assert job["state"] == "running" and job["kind"] == "dispatch"
    manifest = json.loads((v1["root"] / "jobs" / "20260905-100400-live" / "manifest.json").read_text())
    assert manifest["imported"] is True and manifest["imported_external"] is True
    attempt = rows(v1["root"], "SELECT * FROM attempts WHERE job_id='20260905-100400-live'")[0]
    assert json.loads(attempt["evidence_json"])["imported_external"] is True
    assert attempt["state"] == "running"
    assert not rows(v1["root"], "SELECT * FROM artifacts WHERE attempt_id=?",
                    ("20260905-100400-live/a1",))
    cursors = {json.loads(row["data_json"])["store"]: json.loads(row["data_json"])["cursor"]
               for row in rows(v1["root"],
                               "SELECT data_json FROM events WHERE kind='import.cursor'")}
    # Everything the pass did not finish: the run v1 is still running, the one it
    # never finalized, and the one whose lane the roster does not name.
    assert cursors["runs"]["open"] == ["20260905-100300-lost", "20260905-100400-live",
                                       "20260905-100600-nolane"]


def test_the_daemon_never_adopts_an_external_run(v1):
    """principle 3: "v2 never adopts, kills, or finalizes it".

    The flag has to be one the daemon reads, not only one a human can see in
    `manifest.json`: `daemon.imported_external` is what its recovery loop skips on.
    """
    from subfleet import daemon as daemon_module
    run_import(v1)
    live = "20260905-100400-live/a1"
    with Store(v1["root"] / "state.sqlite3", read_only=True) as store:
        every = store.query(daemon_module.LIVE_ATTEMPTS)
    assert live in [row["attempt_id"] for row in every]      # it is live: v1 owns it
    skipped = [row["attempt_id"] for row in every if daemon_module.imported_external(row)]
    assert skipped == [live]                                 # and recovery skips it


def test_an_external_run_settles_when_v1_finalizes_it(v1):
    """principle 3: "It becomes terminal when v1 finalizes it and a later import
    pass reads the rc"."""
    run_import(v1)
    directory = v1["runs"] / "20260905-100400-live"
    meta = json.loads((directory / "meta.json").read_text())
    meta.update(rc=0, finished_at=offset_now(-1), duration_s=12.0)
    write_json(directory / "meta.json", meta)
    (directory / "out.md").write_text("late deliverable\n", encoding="utf-8")
    report = run_import(v1)
    job = rows(v1["root"], "SELECT * FROM jobs WHERE job_id='20260905-100400-live'")[0]
    assert job["state"] == "succeeded" and job["rc"] == 0
    assert job["accepted_attempt_id"] == "20260905-100400-live/a1"
    attempt = rows(v1["root"], "SELECT * FROM attempts WHERE job_id='20260905-100400-live'")[0]
    assert attempt["state"] == "succeeded" and attempt["child_pid"] is None
    evidence = json.loads(attempt["evidence_json"])
    assert evidence["imported_external"] is False and evidence["settled_from_v1"] is True
    manifest = json.loads((v1["root"] / "jobs" / "20260905-100400-live" / "manifest.json").read_text())
    assert manifest["imported_external"] is False      # and the file agrees with the row
    assert rows(v1["root"], "SELECT * FROM artifacts WHERE attempt_id=? AND role='deliverable'",
                ("20260905-100400-live/a1",))
    assert report.stores["runs"].reasons.get("external-run-settled-from-v1") == 1


def test_runs_limit_imports_the_oldest_first_and_never_strands_the_rest(v1):
    """A bounded pass must leave the cursor where a later pass can continue."""
    first = run_import(v1, runs_limit=2)
    assert first.stores["runs"].seen == 2
    imported = {row["job_id"] for row in rows(v1["root"], "SELECT job_id FROM jobs")}
    assert imported == {"20260905-100000-ok", "20260905-100100-limited"}
    second = run_import(v1)
    assert second.stores["runs"].imported >= 3
    assert {row["job_id"] for row in rows(v1["root"], "SELECT job_id FROM jobs")} > imported


def test_a_run_on_an_unknown_lane_is_reported_not_invented(v1):
    """`attempts.lane_id` references a lane; a run on a home v1 no longer has is reported."""
    report = run_import(v1)
    assert report.stores["runs"].reasons.get("no-lane-for-run") == 1
    assert not rows(v1["root"], "SELECT * FROM jobs WHERE job_id='20260905-100600-nolane'")


# --- readings -----------------------------------------------------------------

def test_wham_cache_rows_are_provider_or_stale_by_age(v1):
    """Manifest row `S/capacity-live-cache.json`: label by `probed_at` against
    `READING_TTL_S` (C-9.1)."""
    fresh = run_import(v1)
    labels = {row["label"] for row in rows(v1["root"], "SELECT * FROM readings WHERE source='wham'")}
    assert labels == {"provider"}
    assert fresh.stores["capacity-live-cache"].imported == 2

    stale_root = v1["root"].with_name("stale")
    write_json(v1["state"] / "capacity-live-cache.json", {
        **json.loads((v1["state"] / "capacity-live-cache.json").read_text()),
        "probed_at": offset_now(-importer.READING_TTL_S - 60)})
    import_v1(stale_root, v1_state=v1["state"], delegate_state=v1["delegate"],
              roster_dir=v1["roster"], home=v1["home"])
    labels = {row["label"] for row in rows(stale_root, "SELECT * FROM readings WHERE source='wham'")}
    assert labels == {"stale-provider"}


def test_windows_are_classified_by_duration(v1):
    """C-9.7: 300 minutes is `five_hour`, 10080 is `seven_day`; v1's `weekly` is the latter."""
    run_import(v1)
    windows = {row["window"] for row in rows(v1["root"], "SELECT * FROM readings WHERE source='wham'")}
    assert windows == {"seven_day"}
    reading = rows(v1["root"], "SELECT * FROM readings WHERE source='wham' ORDER BY reading_id")[0]
    assert reading["scope"] == "account" and reading["utilization"] == pytest.approx(0.04)


def test_learned_percentages_are_never_readings(v1):
    """migration.md principle 2: learned percentages are never imported as quota."""
    report = run_import(v1)
    assert report.stores["capacity-live-cache"].reasons["not-a-live-reading"] == 2
    assert report.stores["capacity-live-cache"].reasons["learned-capacity-not-imported"] == 1
    assert not rows(v1["root"], "SELECT * FROM readings WHERE window='five_hour' AND source='wham'")


def test_desktop_oauth_becomes_readings_for_the_desktop_account(v1):
    """Manifest row `S/claude-oauth-raw.json`: readings for the desktop account."""
    run_import(v1)
    readings = rows(v1["root"], "SELECT * FROM readings WHERE source='oauth-usage' "
                                "AND scope='account' ORDER BY window")
    lane = rows(v1["root"], "SELECT * FROM lanes WHERE desktop=1")[0]
    assert {row["window"] for row in readings} == {"five_hour", "seven_day"}
    assert all(row["lane_id"] == lane["lane_id"] for row in readings)
    assert readings[0]["utilization"] == pytest.approx(0.53)   # a percent, not a fraction
    assert readings[1]["utilization"] == pytest.approx(0.46)


def test_scoped_limits_become_admission_observed_rows_without_a_percentage(v1):
    """Manifest row: "per-model scoped limits ... become `admission-observed` rows".

    C-9.1 and plan amendment 15: admission-observed evidence never renders as a
    percentage, so no utilization is stored.
    """
    run_import(v1)
    scoped = rows(v1["root"], "SELECT * FROM readings WHERE label='admission-observed' "
                              "AND source='oauth-usage'")
    assert [row["scope"] for row in scoped] == ["claude-fable-5-1"]
    assert scoped[0]["utilization"] is None
    assert scoped[0]["window"] == "admission"                  # C-9.8


def test_unmapped_payload_windows_are_reported_and_left(v1):
    """A codename window no policy model names is reported, never guessed at."""
    report = run_import(v1)
    assert any("nimbus_quill" in note for note in report.stores["claude-oauth-raw"].notes)
    assert not rows(v1["root"], "SELECT * FROM readings WHERE scope='nimbus_quill'")


def test_keepalive_pings_are_admission_observed_on_haiku(v1):
    """Manifest row `S/keepalive.json`: admission-observed, Haiku scope, never a reset."""
    run_import(v1)
    pings = rows(v1["root"], "SELECT * FROM readings WHERE source='keepalive'")
    assert len(pings) == 1
    assert pings[0]["label"] == "admission-observed"
    assert pings[0]["scope"] == "claude-haiku-4-5-20251001"
    assert pings[0]["resets_at"] is None and pings[0]["utilization"] is None


def test_a_lane_keepalive_never_opened_is_not_evidence(v1):
    run_import(v1)
    report = run_import(v1)
    assert report.stores["keepalive"].reasons.get("cursor-unchanged") == 1


# --- actions and closures -----------------------------------------------------

@pytest.mark.parametrize("home_name", ["/missing/.codex-1", "~/.codex-8", "relative/codex-home"])
def test_unknown_reset_home_is_reported_without_fabricating_account_action(v1, home_name):
    write_json(v1["state"] / "reset-policy.json", {
        "lane": home_name, "credit_id": "unbound-credit", "last_redeemed_at": offset_now(-3600),
        "last_redemptions": {home_name: offset_now(-3600)},
    })
    report = run_import(v1)
    assert not rows(v1["root"], "SELECT * FROM actions WHERE kind='reset-credit'")
    assert report.stores["reset-policy"].reasons["reset-home-without-account-binding"] == 1
    assert "no unambiguous account binding" in report.stores["reset-policy"].notes[0]


def test_reset_redemptions_become_confirmed_actions(v1):
    """Manifest row `S/reset-policy.json`: kind reset-credit, state confirmed,
    op_key = account key plus credit id (C-19.1)."""
    run_import(v1)
    actions = rows(v1["root"], "SELECT * FROM actions ORDER BY op_key")
    assert len(actions) == 2
    assert {row["kind"] for row in actions} == {"reset-credit"}
    assert {row["state"] for row in actions} == {"confirmed"}
    with_credit = [row for row in actions if "RateLimitResetCredit" in row["op_key"]]
    assert with_credit[0]["op_key"] == (
        f"codex:{CODEX_ONE}:RateLimitResetCredit_26c531af84688191afcbab1b15c7ec69")
    without = [row for row in actions if row not in with_credit][0]
    assert json.loads(without["request_json"])["credit_id_absent_in_v1"] is True


def test_reimport_preserves_existing_legacy_prefixed_credit_evidence(v1):
    """Older imports keep their identity; a corrected importer cannot duplicate them."""
    run_import(v1)
    with Store(v1["root"] / "state.sqlite3") as store:
        # Model a database produced by the previous importer, including its IDs.
        with store.transaction("test.legacy-import") as conn:
            conn.execute("UPDATE actions SET action_id='reset-credit:'||action_id, "
                         "op_key='reset-credit:'||op_key")
        before = store.query("SELECT * FROM actions ORDER BY action_id")
    # Even a changed timestamp must not create a second action for this credit.
    policy = json.loads((v1["state"] / "reset-policy.json").read_text())
    policy["last_redeemed_at"] = offset_now(-3000)
    write_json(v1["state"] / "reset-policy.json", policy)
    run_import(v1)
    assert rows(v1["root"], "SELECT * FROM actions ORDER BY action_id") == before


def test_imported_credit_cannot_be_spent_after_its_interval_and_override_expire(v1):
    from subfleet.actions import ResetCredits

    now = datetime.now(timezone.utc)
    policy = json.loads((v1["state"] / "reset-policy.json").read_text())
    old = (now - timedelta(days=8)).isoformat()
    policy["last_redeemed_at"] = old
    policy["last_redemptions"] = {policy["lane"]: old}
    write_json(v1["state"] / "reset-policy.json", policy)
    run_import(v1)
    calls = []

    class Credits:
        def list_reset_credits(self, *args, **kwargs):
            calls.append("list")
            return {"status": "ok", "credits": [{"id": policy["credit_id"],
                    "reset_type": "codex_rate_limits", "status": "available"}]}

        def consume_reset_credit(self, *args, **kwargs):
            pytest.fail("a previously redeemed imported credit must never be consumed")

    with Store(v1["root"] / "state.sqlite3") as store:
        row = store.one("SELECT * FROM lanes WHERE account_key=?", (f"codex:{CODEX_ONE}",))
        store.update_lane(row["lane_id"], owner="v2")
        row.update(owner="v2", dispatchable=False, probe={"status": "limited", "limit_reached": True,
                   "checked_at": now.isoformat(), "account_key": row["account_key"]})
        resets = ResetCredits(store, {}, lambda lane: Credits())
        assert resets.confirmed_override(row["lane_id"], now=now) is None
        assert resets.evaluate({"lanes": [row]}, now=now)["status"] == "no-concrete-credit"
        assert len(store.query("SELECT * FROM actions")) == 1
    assert calls == ["list"]


def test_a_redemption_is_one_action_however_v1_remembers_it(v1):
    """C-19.1: `op_key` is unique per operation; v1 keeps one credit id at a time."""
    home = v1["home"]
    run_import(v1)
    before = {row["subject"] + row["created_at"] for row in
              rows(v1["root"], "SELECT * FROM actions")}
    policy = json.loads((v1["state"] / "reset-policy.json").read_text())
    # v1 redeems on another home: the first redemption loses its credit id.
    policy["last_redemptions"][f"{home}/.codex-2"] = offset_now(-100)
    policy["lane"] = f"{home}/.codex-2"
    policy["credit_id"] = "RateLimitResetCredit_second"
    write_json(v1["state"] / "reset-policy.json", policy)
    run_import(v1)
    after = rows(v1["root"], "SELECT * FROM actions")
    assert len({row["subject"] + row["created_at"] for row in after} - before) == 1
    assert len(after) == 3          # not four: no redemption is counted twice


def test_a_tickle_updated_since_the_last_pass_is_imported(v1):
    """Manifest row `S/tickles/`: the timestamps are what stop a re-nudge."""
    run_import(v1, milestone=6)
    path = v1["state"] / "tickles" / f"{SESSION}.json"
    entry = json.loads(path.read_text())
    entry["at"] = offset_now(-1)
    write_json(path, entry)
    os.utime(path, None)
    report = run_import(v1, milestone=6)
    assert report.stores["sessions-kit"].imported == 1
    latest = [json.loads(row["data_json"])["at"] for row in
              rows(v1["root"], "SELECT * FROM events WHERE kind='tickle' ORDER BY event_id")]
    assert len(latest) == 2 and latest[1] > latest[0]


def test_cooldowns_become_closures_with_scope_and_source(v1):
    """Manifest row `D/cooldowns.json`: scope, clock source, `source_event: v1-cooldown`."""
    run_import(v1)
    closures = rows(v1["root"], "SELECT * FROM closures ORDER BY closure_id")
    assert {row["source_event"] for row in closures} == {"v1-cooldown"}
    assert {row["reason"] for row in closures} == {"cooldown"}
    scopes = {row["scope"] for row in closures}
    assert scopes == {"account", "claude-opus-5"}          # "*" is account; "sonnet" expired
    assert {row["clock_source"] for row in closures} <= {"reported", "guessed"}


def test_expired_cooldowns_are_skipped(v1):
    """Manifest row `D/cooldowns.json`: "expired entries skipped"."""
    report = run_import(v1)
    assert report.stores["cooldowns"].reasons.get("expired") == 1
    assert not rows(v1["root"], "SELECT * FROM closures WHERE scope='claude-sonnet-5'")


def test_a_cooldown_matching_a_provider_reset_is_reported_not_guessed(v1):
    """C-9.4: a clock is `reported` only on evidence that a provider reported it."""
    reading = rows
    write_json(v1["delegate"] / "cooldowns.json", {
        f"{v1['home']}/.codex-2": {"*": json.loads(
            (v1["state"] / "capacity-live-cache.json").read_text())["accounts"][1]["weekly"]["reset_at"]}})
    run_import(v1)
    closure = reading(v1["root"], "SELECT * FROM closures")[0]
    assert closure["clock_source"] == "reported"


def test_a_cooldown_with_no_corroborating_reading_is_guessed(v1):
    write_json(v1["delegate"] / "cooldowns.json", {ENROLLED: {"*": offset_now(3600)}})
    run_import(v1)
    closure = rows(v1["root"], "SELECT * FROM closures")[0]
    assert closure["clock_source"] == "guessed"


def test_a_closure_is_extended_never_shortened(v1):
    """C-9.6: a new closure on the same lane and scope extends `until`."""
    run_import(v1)
    before = rows(v1["root"], "SELECT * FROM closures WHERE scope='account'")[0]
    write_json(v1["delegate"] / "cooldowns.json", {
        f"{v1['home']}/.codex-2": {"*": offset_now(60)}})
    run_import(v1)
    after = rows(v1["root"], "SELECT * FROM closures WHERE closure_id=?", (before["closure_id"],))[0]
    assert after["until_at"] == before["until_at"]


# --- notices ------------------------------------------------------------------

def test_notices_are_pending_or_surfaced_with_the_session_from_the_file_name(v1):
    """Manifest row `S/notices/`: pending when unsurfaced, surfaced otherwise."""
    run_import(v1)
    notices = rows(v1["root"], "SELECT * FROM notices WHERE transport IS NULL ORDER BY notice_id")
    assert [row["state"] for row in notices] == ["surfaced", "pending"]
    assert {row["session_id"] for row in notices} == {SESSION}
    assert notices[0]["job_id"] == "20260905-100000-ok"


def test_a_notice_for_a_run_outside_the_ledger_is_reported(v1):
    report = run_import(v1)
    assert report.stores["notices"].reasons.get("unknown-job") == 2


def test_a_notice_lands_once_its_run_is_imported(v1):
    """The cursor keeps an unresolved line for the next pass (principle 4)."""
    late = "20260905-101000-late"
    run_import(v1)
    assert not rows(v1["root"], "SELECT * FROM notices WHERE job_id=?", (late,))
    directory = v1["runs"] / late
    directory.mkdir()
    write_json(directory / "meta.json",
               run_meta(late, rc=0, lane=str(v1["home"] / ".codex-1"),
                        codex_home=str(v1["home"] / ".codex-1")))
    (directory / "prompt.md").write_text("late\n", encoding="utf-8")
    report = run_import(v1)
    assert report.stores["notices"].imported == 1
    assert rows(v1["root"], "SELECT * FROM notices WHERE job_id=?", (late,))
    # The one whose run is older than the retained ledger stays on the retry list.
    assert report.stores["notices"].reasons.get("unknown-job") == 1


# --- the legacy cockpit (C-30.4, design §13) -----------------------------------

def run_legacy(v1: dict, **kwargs):
    """A milestone-9 pass, the one that imports the legacy cockpit rows."""
    kwargs.setdefault("milestone", importer.LEGACY_MILESTONE)
    kwargs.setdefault("claude_projects", v1["claude"] / "projects")
    return run_import(v1, **kwargs)


def conversations(root: Path, sql: str, params: tuple = ()) -> list[dict]:
    connection = sqlite3.connect(f"file:{root / 'conversations.sqlite3'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def dispositions(report, key: str = "outbox") -> dict[str, str]:
    """Each message's or journal entry's disposition, by id."""
    return {item["message_id"]: item["disposition"] for item in report.stores[key].items
            if item["source"] != "conversation"}


def fences(report) -> list[dict]:
    """The conversations a pass blocked or released (C-30.4)."""
    return [item for item in report.stores["outbox"].items if item["source"] == "conversation"]


def bound(cid: str, disposition: str, legacy_hold: str | None, *, blocked_by: str | None = None,
          unsettled: tuple = (), session: str = LEGACY_SESSION) -> dict:
    """A fence item: the conversation's legacy hold beside its own block, and its
    messages that are not settled (C-30.4)."""
    return {"source": "conversation", "conversation_id": cid, "session_id": f"claude:{session}",
            "disposition": disposition, "legacy_hold": legacy_hold, "blocked_by": blocked_by,
            "unsettled": [{"message_id": message_id, "state": state} for message_id, state in unsettled],
            "detail": legacy_hold or legacy.RELEASED}


#: The fixture's own three outbox rows, as the v1 fixture writes them.
def fixture_rows() -> list[tuple]:
    return [outbox_row(LEGACY[0], LEGACY_SESSION, "finished", "he replied", at=300),
            outbox_row(LEGACY[1], LEGACY_SESSION, "finished", "and a follow-up", at=200),
            outbox_row(LEGACY[2], QUEUED_SESSION, "queued", "still queued", at=100)]


PERSON_SETTINGS = {"model": "opus", "permission": "ask"}


def test_the_outbox_is_the_cockpits_and_never_becomes_notices(v1):
    """C-30.4: the notice mapping is gone. Before milestone 9 the two cockpit rows
    are staged and nothing is written anywhere for them."""
    report = run_import(v1)
    assert report.stores["outbox"].disposition == "staged-milestone-9"
    assert report.stores["cockpit"].disposition == "retain-until-milestone-9"
    assert not rows(v1["root"], "SELECT * FROM notices WHERE transport='v1-socket'")
    assert not (v1["root"] / "conversations.sqlite3").exists()
    report = run_legacy(v1)
    assert report.stores["outbox"].disposition == "import"
    assert report.stores["cockpit"].disposition == "retain"
    assert not rows(v1["root"], "SELECT * FROM notices WHERE transport='v1-socket'")


def test_terminal_messages_become_history_of_one_legacy_conversation(v1):
    """C-30.4, C-24.1, design D-9: one `legacy` conversation per session whose
    transcript is found, bound to its native id, in the transcript's cwd with the
    settings `conversation.open` would give it; its messages are terminal history
    rows under their legacy ids, in legacy order, with their text published 0600."""
    report = run_legacy(v1)
    found = conversations(v1["root"], "SELECT * FROM conversations")
    assert len(found) == 1
    conversation = found[0]
    assert conversation["origin"] == "legacy" and conversation["provider"] == "claude"
    assert conversation["native_session_id"] == LEGACY_SESSION
    assert conversation["workspace"] == str(v1["workspace"])
    assert conversation["blocked_by"] is None and conversation["archived_at"] is None
    assert json.loads(conversation["settings_json"]) == {
        "model": "claude-opus-5-5", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}
    history = conversations(v1["root"], "SELECT * FROM messages ORDER BY seq")
    assert [row["message_id"] for row in history] == list(LEGACY[:2])
    assert [row["seq"] for row in history] == [1, 2]
    for row, text in zip(history, ("he replied", "and a follow-up")):
        assert row["origin"] == "legacy" and row["state"] == "complete"
        assert row["state_reason"] == "legacy-finished"
        assert row["job_id"] is None and row["after_message_id"] is None and row["turn_seq"] == 0
        assert row["turn_ref"] == row["message_id"]          # the cockpit's native user uuid
        assert json.loads(row["attachments_json"]) == []
        assert json.loads(row["settings_json"])["model"] is None   # the outbox never recorded one
        path = Path(row["text_path"])
        assert path.read_text(encoding="utf-8") == text
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.is_relative_to(v1["root"] / "conversations" / conversation["conversation_id"])
    assert report.stores["outbox"].imported == 2
    assert report.stores["outbox"].reasons["history-complete"] == 2
    assert report.stores["outbox"].reasons["legacy-conversations-created"] == 1
    assert dispositions(report) == {LEGACY[0]: "history", LEGACY[1]: "history", LEGACY[2]: "legacy-owned"}


def test_each_message_is_classified_by_itself(v1):
    """C-30.4: terminal statuses are the cockpit's own (finished, error,
    cancelled); a handled unknown delivery settles `failed` as a v2 resolution
    does; everything else, and every message a pass cannot place, is reported
    with its disposition and not dropped."""
    held, missing, done = (str(uuid) for uuid in (
        "0a0a0a0a-0000-4000-8000-000000000001", "0a0a0a0a-0000-4000-8000-000000000002",
        "0a0a0a0a-0000-4000-8000-000000000003"))
    write_outbox(v1["state"], [
        outbox_row(LEGACY[0], LEGACY_SESSION, "finished", "done", at=900),
        outbox_row(LEGACY[1], LEGACY_SESSION, "error", "failed", at=800),
        outbox_row(LEGACY[2], LEGACY_SESSION, "cancelled", "withdrawn", at=700),
        outbox_row(LEGACY[3], LEGACY_SESSION, "cancelled", "handled", at=600, receipt={
            "ok": True, "error": None, "message": "Marked handled by you; no replay.",
            "resolution": "handled"}),
        outbox_row(LEGACY[4], missing, "finished", "no transcript", at=500),
        outbox_row(LEGACY[5], held, "delivery-unknown", "ambiguous", at=400),
        outbox_row(LEGACY[6], held, "finished", "held with it", at=300),
        outbox_row(LEGACY[7], done, "failed", "never a cockpit status", at=200),
        outbox_row(LEGACY[8], "019a0c6e-1b2c-7d3e-8f40-5a6b7c8d9e0f", "finished", "codex", at=100,
                   provider="codex"),
        outbox_row(LEGACY[9], LEGACY_SESSION, "dispatched", "running elsewhere", at=50),
    ])
    write_transcript(v1["claude"], held, v1["workspace"])
    write_transcript(v1["claude"], done, v1["workspace"])
    report = run_legacy(v1)
    assert dispositions(report) == {
        LEGACY[0]: "session-held-by-legacy-owner", LEGACY[1]: "session-held-by-legacy-owner",
        LEGACY[2]: "session-held-by-legacy-owner", LEGACY[3]: "session-held-by-legacy-owner",
        LEGACY[4]: "transcript-not-found", LEGACY[5]: "legacy-owned",
        LEGACY[6]: "session-held-by-legacy-owner", LEGACY[7]: "legacy-owned",
        LEGACY[8]: "not-a-claude-session", LEGACY[9]: "legacy-owned"}
    items = {item["message_id"]: item for item in report.stores["outbox"].items}
    assert items[LEGACY[7]]["detail"] == "a status the cockpit never wrote"
    assert items[LEGACY[5]]["status"] == "delivery-unknown"
    assert report.stores["outbox"].imported == 0 and report.stores["outbox"].seen == 10
    assert not (v1["root"] / "conversations.sqlite3").exists() or not conversations(
        v1["root"], "SELECT * FROM messages")

    # The dispatched message finishes in v1: its session's four settle by status.
    write_outbox(v1["state"], [
        outbox_row(LEGACY[0], LEGACY_SESSION, "finished", "done", at=900),
        outbox_row(LEGACY[1], LEGACY_SESSION, "error", "failed", at=800),
        outbox_row(LEGACY[2], LEGACY_SESSION, "cancelled", "withdrawn", at=700),
        outbox_row(LEGACY[3], LEGACY_SESSION, "cancelled", "handled", at=600, receipt={
            "ok": True, "error": None, "message": "Marked handled by you; no replay.",
            "resolution": "handled"}),
        outbox_row(LEGACY[9], LEGACY_SESSION, "finished", "running elsewhere", at=50),
    ])
    report = run_legacy(v1)
    settled = {row["message_id"]: (row["state"], row["state_reason"], row["seq"]) for row in conversations(
        v1["root"], "SELECT * FROM messages")}
    assert settled == {LEGACY[0]: ("complete", "legacy-finished", 1),
                       LEGACY[1]: ("failed", "legacy-error: provider-failed", 2),
                       LEGACY[2]: ("cancelled", "legacy-cancelled", 3),
                       LEGACY[3]: ("failed", "legacy-handled", 4),
                       LEGACY[9]: ("complete", "legacy-finished", 5)}
    assert report.stores["outbox"].imported == 5


def test_only_a_uuid_session_id_is_looked_up_as_a_transcript(v1):
    """C-30.4: a Claude session id is a UUID; anything else is reported, and never
    becomes part of a path."""
    write_outbox(v1["state"], [outbox_row(LEGACY[0], "../../elsewhere", "finished", "x", at=60)])
    report = run_legacy(v1)
    assert dispositions(report) == {LEGACY[0]: "not-a-claude-session"}


def test_a_repeated_import_creates_nothing_new(v1):
    """C-30.4, migration.md principle 4: idempotent by legacy message id."""
    first = run_legacy(v1)
    tables = ("conversations", "messages")
    before = {table: conversations(v1["root"], f"SELECT * FROM {table} ORDER BY 1") for table in tables}
    second = run_legacy(v1)
    assert {table: conversations(v1["root"], f"SELECT * FROM {table} ORDER BY 1") for table in tables} == before
    assert first.stores["outbox"].imported == 2 and second.stores["outbox"].imported == 0
    assert dispositions(second) == {LEGACY[0]: "already-imported", LEGACY[1]: "already-imported",
                                    LEGACY[2]: "legacy-owned"}
    assert second.stores["outbox"].cursor == {"mapping": "legacy-history", "rows": 3, "max_sequence": 3}


def test_a_message_without_a_transcript_lands_when_the_transcript_appears(v1):
    """C-30.4: a terminal message whose transcript is not found is reported, not
    dropped, and a later pass imports it."""
    missing = "0b0b0b0b-0000-4000-8000-000000000001"
    write_outbox(v1["state"], [outbox_row(LEGACY[0], missing, "finished", "later", at=60)])
    report = run_legacy(v1)
    item = report.stores["outbox"].items[0]
    assert item["disposition"] == "transcript-not-found" and item["session_id"] == f"claude:{missing}"
    assert any("transcript was not found" in note for note in report.stores["outbox"].notes)
    write_transcript(v1["claude"], missing, v1["workspace"])
    assert dispositions(run_legacy(v1)) == {LEGACY[0]: "history"}


def test_a_session_that_cannot_continue_here_is_reported_not_bound(v1):
    """C-30.2, C-30.4, IR-15: the conversation is created only as `conversation.open`
    would create it; a session whose cwd is gone waits, reported."""
    gone = v1["workspace"] / "gone"
    gone.mkdir()
    write_outbox(v1["state"], [outbox_row(LEGACY[0], LEGACY_SESSION, "finished", "x", at=60)])
    for path in (v1["claude"] / "projects").rglob(f"{LEGACY_SESSION}.jsonl"):
        path.unlink()
    write_transcript(v1["claude"], LEGACY_SESSION, gone)
    gone.rmdir()
    report = run_legacy(v1)
    item = report.stores["outbox"].items[0]
    assert item["disposition"] == "session-not-continuable"
    assert item["detail"] == "its working directory no longer exists"
    assert not conversations(v1["root"], "SELECT * FROM conversations")


def test_history_never_lands_after_a_conversations_own_messages(v1):
    """C-30.4, C-24.2: a session that already has a conversation with messages of
    its own gets no history appended after them; it is reported."""
    v1["root"].mkdir(parents=True, exist_ok=True)
    store = ConversationStore(v1["root"])
    try:
        conversation, _ = store.create_conversation(
            provider="claude", workspace=str(v1["workspace"]), workspace_kind="in-place",
            settings={"model": "opus", "permission": "ask"}, origin="native", native_session_id=LEGACY_SESSION)
        store.submit_message(conversation_id=conversation["conversation_id"],
                             message_id="0c0c0c0c-0000-4000-8000-000000000001", after_message_id=None,
                             text="a turn of its own", attachments=[], settings={"model": "opus", "permission": "ask"})
    finally:
        store.close()
    report = run_legacy(v1)
    assert dispositions(report)[LEGACY[0]] == "conversation-has-own-messages"
    assert [row["origin"] for row in conversations(v1["root"], "SELECT origin FROM messages")] == ["person"]


def test_the_client_journal_keeps_its_legacy_owner_and_so_does_its_session(v1):
    """C-30.4: every pending-messages.json entry is reported and never sent; the
    session it names stays with the legacy writer."""
    pending = "0d0d0d0d-0000-4000-8000-000000000001"
    write_json(v1["state"] / "cockpit-client" / "pending-messages.json", {
        f"claude:{LEGACY_SESSION}": {
            "request": {"op": "enqueue", "message_id": pending, "session_id": f"claude:{LEGACY_SESSION}",
                        "prompt": "journal prompt text", "image_paths": []},
            "sourceImagePaths": []}})
    (v1["state"] / "cockpit-client" / f"images-{pending}").mkdir()
    report = run_legacy(v1)
    assert report.stores["cockpit"].items == [{
        "source": "client-journal", "message_id": pending, "session_id": f"claude:{LEGACY_SESSION}",
        "status": None, "disposition": "legacy-owned",
        "detail": "an unacknowledged cockpit send; the import never sends it"}]
    assert report.stores["cockpit"].skipped == 1
    assert any("image snapshot folders" in note for note in report.stores["cockpit"].notes)
    assert set(dispositions(report).values()) == {"session-held-by-legacy-owner", "legacy-owned"}
    assert not conversations(v1["root"], "SELECT * FROM messages")
    payload = json.dumps(report.as_dict())
    assert "journal prompt text" not in payload and "he replied" not in payload   # ids, never prompts


def test_a_journal_entry_holds_its_session_whatever_the_outbox_says_of_its_id(v1):
    """C-30.4: every journal entry holds its session, even one whose id the outbox
    shows finished; the report carries that outbox status beside the entry."""
    write_json(v1["state"] / "cockpit-client" / "pending-messages.json", {
        f"claude:{LEGACY_SESSION}": {
            "request": {"op": "enqueue", "message_id": LEGACY[1], "session_id": f"claude:{LEGACY_SESSION}",
                        "prompt": "and a follow-up", "image_paths": []},
            "sourceImagePaths": []}})
    report = run_legacy(v1)
    assert report.stores["cockpit"].items == [{
        "source": "client-journal", "message_id": LEGACY[1], "session_id": f"claude:{LEGACY_SESSION}",
        "status": "finished", "disposition": "legacy-owned",
        "detail": "an unacknowledged cockpit send; the import never sends it"}]
    assert dispositions(report) == {LEGACY[0]: "session-held-by-legacy-owner",
                                    LEGACY[1]: "session-held-by-legacy-owner", LEGACY[2]: "legacy-owned"}
    items = {item["message_id"]: item for item in report.stores["outbox"].items}
    assert items[LEGACY[0]]["detail"] == f"the cockpit journal holds an unacknowledged send {LEGACY[1]}"
    assert not conversations(v1["root"], "SELECT * FROM conversations")


def _unreadable_journal(path: Path, kind: str) -> str:
    """Make the journal unreadable one way; return the problem `read_journal` names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "not json":
        path.write_text("{not json", encoding="utf-8")
        return "unreadable: JSONDecodeError"
    if kind == "not an object":
        path.write_text("[]", encoding="utf-8")
        return "not an object keyed by session"
    if kind == "keyed by something else":                # review L3: holds sessions named `version` and `entries`
        path.write_text(json.dumps({"version": 2, "entries": {f"claude:{LEGACY_SESSION}": {}}}), encoding="utf-8")
        return "not an object keyed by session"
    if kind == "a request naming no session":
        path.write_text(json.dumps({f"claude:{LEGACY_SESSION}": {"request": {"session_id": "nobody"}}}),
                        encoding="utf-8")
        return "not an object keyed by session"
    path.mkdir()                                           # reading it raises IsADirectoryError, an OSError
    return "unreadable: IsADirectoryError"


def _remove(path: Path) -> None:
    if path.is_dir():
        path.rmdir()
    else:
        path.unlink()


@pytest.mark.parametrize("kind", ["not json", "not an object", "keyed by something else",
                                  "a request naming no session", "a directory"])
def test_an_unreadable_journal_holds_every_session(v1, kind):
    """C-30.4: a journal that cannot be read could name any session, so it is
    never read as empty. No session is bound while it is unreadable, a session
    an earlier pass bound is held (`legacy_hold`), and the first pass that can
    read it again imports and releases as usual."""
    journal = v1["state"] / legacy.JOURNAL
    problem = _unreadable_journal(journal, kind)
    held = legacy.journal_hold(problem)
    report = run_legacy(v1)
    assert report.stores["cockpit"].reasons == {"unreadable-journal": 1}
    items = {item["message_id"]: item for item in report.stores["outbox"].items}
    assert {key: (item["disposition"], item.get("detail")) for key, item in items.items()} == {
        LEGACY[0]: ("session-held-by-legacy-owner", held), LEGACY[1]: ("session-held-by-legacy-owner", held),
        LEGACY[2]: ("legacy-owned", None)}
    assert any("every session is held" in note for note in report.stores["outbox"].notes)
    assert not conversations(v1["root"], "SELECT * FROM conversations")

    _remove(journal)                                      # the journal is gone: nothing is pending
    assert dispositions(run_legacy(v1)) == {LEGACY[0]: "history", LEGACY[1]: "history", LEGACY[2]: "legacy-owned"}
    (cid,) = [row["conversation_id"] for row in conversations(v1["root"], "SELECT * FROM conversations")]

    _unreadable_journal(journal, kind)                    # unreadable again: the bound session is fenced
    report = run_legacy(v1)
    assert fences(report) == [bound(cid, "bound-session-held", held)]
    assert conversations(v1["root"], "SELECT blocked_by, legacy_hold FROM conversations") == [
        {"blocked_by": None, "legacy_hold": held}]

    _remove(journal)
    journal.write_text("{}", encoding="utf-8")            # readable and empty: the hold lifts
    report = run_legacy(v1)
    assert fences(report) == [bound(cid, "bound-session-released", None)]
    assert report.stores["cockpit"].reasons == {}
    assert conversations(v1["root"], "SELECT blocked_by, legacy_hold FROM conversations") == [
        {"blocked_by": None, "legacy_hold": None}]


def _bind(v1) -> str:
    """Pass 1: the fixture binds LEGACY_SESSION; its conversation's id."""
    run_legacy(v1)
    (cid,) = [row["conversation_id"] for row in conversations(v1["root"], "SELECT * FROM conversations")]
    return cid


def _hold_of(v1, cid: str) -> str | None:
    return conversations(v1["root"], "SELECT legacy_hold FROM conversations WHERE conversation_id=?",
                         (cid,))[0]["legacy_hold"]


def _damage_outbox(state: Path, kind: str) -> str:
    """Make the outbox unreadable one way; return the reason the pass reports."""
    path = state / "outbox.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    if kind == "corrupt":
        path.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
        return "unreadable-database"
    connection = sqlite3.connect(path)
    if kind == "no messages table":
        connection.execute("CREATE TABLE other (x)")
    else:                                                  # the cockpit's table name, other columns
        connection.execute("CREATE TABLE messages (sequence INTEGER PRIMARY KEY, message_id TEXT)")
        connection.execute("INSERT INTO messages(message_id) VALUES ('x')")
    connection.commit()
    connection.close()
    return "no-messages-table" if kind == "no messages table" else "unreadable-database"


@pytest.mark.parametrize("kind", ["corrupt", "no messages table", "other columns"])
def test_an_outbox_that_cannot_be_read_holds_every_session(v1, kind):
    """C-30.4 (review M2): an outbox that exists and cannot be read could hold a
    message in flight in any session, so it is never read as empty. Every bound
    conversation is held, the journal is still read, a corrupt file is named
    `unreadable-database`, and the first pass that reads the outbox again
    releases the hold."""
    cid = _bind(v1)
    label = _damage_outbox(v1["state"], kind)
    report = run_legacy(v1)
    assert report.stores["outbox"].reasons == {label: 1, "bound-session-held": 1}
    held = legacy.outbox_hold(label)
    assert _hold_of(v1, cid) == held
    assert fences(report) == [bound(cid, "bound-session-held", held)]
    assert any(f"outbox.sqlite3 is {label}" in note for note in report.stores["outbox"].notes)

    write_outbox(v1["state"], fixture_rows())
    report = run_legacy(v1)
    assert fences(report) == [bound(cid, "bound-session-released", None)]
    assert _hold_of(v1, cid) is None


def test_a_missing_outbox_still_reads_the_journal_and_fences(v1):
    """C-30.4 (review M2): with no outbox the journal and the fence still run. A
    missing outbox holds nothing by itself; an unreadable journal, or one naming
    the session, still holds its bound conversation, and an empty one releases it."""
    cid = _bind(v1)
    (v1["state"] / "outbox.sqlite3").unlink()
    journal = v1["state"] / legacy.JOURNAL
    problem = _unreadable_journal(journal, "not json")
    report = run_legacy(v1)
    assert report.stores["outbox"].reasons == {"absent": 1, "bound-session-held": 1}
    assert fences(report) == [bound(cid, "bound-session-held", legacy.journal_hold(problem))]
    pending = "0d0d0d0d-0000-4000-8000-000000000002"
    journal.write_text(json.dumps({f"claude:{LEGACY_SESSION}": {"request": {
        "op": "enqueue", "message_id": pending, "session_id": f"claude:{LEGACY_SESSION}", "prompt": "x",
        "image_paths": []}}}), encoding="utf-8")
    report = run_legacy(v1)
    assert fences(report) == [bound(cid, "bound-session-held",
                                    f"the cockpit journal holds an unacknowledged send {pending}")]
    journal.write_text("{}", encoding="utf-8")
    report = run_legacy(v1)
    assert fences(report) == [bound(cid, "bound-session-released", None)]
    assert report.stores["outbox"].reasons == {"absent": 1, "bound-session-released": 1}


def test_a_pass_with_nothing_to_import_hold_or_fence_creates_no_store(v1):
    """C-30.4: with no outbox message, nothing held and no conversation store, a
    pass opens no store."""
    (v1["state"] / "outbox.sqlite3").unlink()
    journal = v1["state"] / legacy.JOURNAL
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{}", encoding="utf-8")
    report = run_legacy(v1)
    assert report.stores["outbox"].reasons == {"absent": 1}
    assert not (v1["root"] / "conversations.sqlite3").exists()


def _recorded(v1) -> dict[str, str]:
    return {row["session_key"]: row["reason"]
            for row in conversations(v1["root"], "SELECT session_key, reason FROM legacy_sessions")}


@pytest.mark.parametrize("kind", ["unreadable outbox", "journal entry", "unreadable journal", "broker",
                                  "live worker"])
def test_a_first_pass_that_holds_a_session_records_it(v1, kind):
    """C-30.4 (second review, finding 1): a first pass with no outbox message and
    no conversation store still records what it holds, so a session opened
    afterwards is bound held."""
    handle = None
    if kind == "unreadable outbox":
        _damage_outbox(v1["state"], "corrupt")
        expected = {"*": legacy.outbox_hold("unreadable-database")}
    elif kind == "journal entry":
        (v1["state"] / "outbox.sqlite3").unlink()
        write_json(v1["state"] / legacy.JOURNAL, {f"claude:{LEGACY_SESSION}": {"request": {
            "message_id": LEGACY[5], "session_id": f"claude:{LEGACY_SESSION}"}}})
        expected = {f"claude:{LEGACY_SESSION}": f"the cockpit journal holds an unacknowledged send {LEGACY[5]}"}
    elif kind == "unreadable journal":
        (v1["state"] / "outbox.sqlite3").unlink()
        expected = {"*": legacy.journal_hold(_unreadable_journal(v1["state"] / legacy.JOURNAL, "not json"))}
    elif kind == "broker":
        (v1["state"] / "outbox.sqlite3").unlink()
        handle = os.open(v1["state"] / "broker.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        expected = {"*": "the cockpit broker holds broker.lock, so it may dispatch into any session"}
    else:
        (v1["state"] / "outbox.sqlite3").unlink()
        handle = _sleeper()
        write_json(v1["state"] / "native-workers.json", {f"claude:{LEGACY_SESSION}": {"pid": handle.pid}})
        expected = {f"claude:{LEGACY_SESSION}": f"the cockpit's worker pid {handle.pid} is live in it"}
    try:
        run_legacy(v1)
    finally:
        if isinstance(handle, subprocess.Popen):
            handle.kill()
            handle.wait()
        elif handle is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)
    assert _recorded(v1) == expected
    store = ConversationStore(v1["root"])
    try:
        opened, _ = store.create_conversation(provider="claude", workspace=str(v1["workspace"]),
                                              workspace_kind="in-place", settings=PERSON_SETTINGS,
                                              origin="native", native_session_id=LEGACY_SESSION)
    finally:
        store.close()
    assert opened["legacy_hold"] == next(iter(expected.values()))


def test_a_later_pass_forgets_the_sessions_an_earlier_one_held(v1):
    """C-30.4: each pass replaces the held sessions it records; one that finds a
    session settled records nothing for it, so a conversation opened on it is
    not held. A Codex thread is recorded by its thread id."""
    thread = "0199a5f3-0000-7000-8000-00000000c0de"
    write_outbox(v1["state"], [*fixture_rows(), outbox_row(LEGACY[3], f"app:{thread}", "dispatched", "codex",
                                                           provider="codex", at=50)])
    run_legacy(v1)
    assert _recorded(v1) == {f"claude:{QUEUED_SESSION}": f"message {LEGACY[2]} is queued",
                             f"codex:{thread}": f"message {LEGACY[3]} is dispatched"}

    def open_codex(request_id: str) -> dict:
        store = ConversationStore(v1["root"])
        try:
            return store.create_conversation(provider="codex", workspace=str(v1["workspace"]),
                                             workspace_kind="in-place",
                                             settings={"model": "gpt-6-astra", "permission": "read-only"},
                                             origin="native", native_session_id=thread.upper(), lane_id="codex-1",
                                             request_id=request_id)[0]
        finally:
            store.close()
    held = open_codex("r-1")
    assert held["legacy_hold"] == f"message {LEGACY[3]} is dispatched"          # bound held, in one spelling
    store = ConversationStore(v1["root"])
    try:
        store.query("UPDATE conversations SET native_session_id=NULL WHERE conversation_id=?",
                    (held["conversation_id"],))                               # set it aside for the next open
    finally:
        store.close()
    write_outbox(v1["state"], [outbox_row(LEGACY[0], LEGACY_SESSION, "finished", "he replied", at=300)])
    run_legacy(v1)
    assert _recorded(v1) == {}
    assert open_codex("r-2")["legacy_hold"] is None


def _sleeper(**env: str) -> subprocess.Popen:
    """A live process; `env` replaces the environment, so it carries no Subfleet marker unless given one."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env})


def test_a_running_cockpit_broker_holds_every_session(v1):
    """C-30.4 (review M1): the broker holds `S/broker.lock` for its lifetime and
    may dispatch into any session, so while another process holds it every
    session is held, and a bound one fenced. The probe is a shared lock on a
    read-only handle: it writes nothing, and creates no lock file."""
    cid = _bind(v1)
    lock = v1["state"] / "broker.lock"
    handle = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    os.write(handle, b"broker")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = (lock.read_bytes(), lock.stat().st_mtime_ns)
    try:
        report = run_legacy(v1)
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
    held = "the cockpit broker holds broker.lock, so it may dispatch into any session"
    assert fences(report) == [bound(cid, "bound-session-held", held)]
    assert any(held in note for note in report.stores["outbox"].notes)
    assert (lock.read_bytes(), lock.stat().st_mtime_ns) == before
    report = run_legacy(v1)                                  # the broker has stopped
    assert fences(report) == [bound(cid, "bound-session-released", None)]
    lock.unlink()
    run_legacy(v1)
    assert not lock.exists()


def test_a_live_cockpit_worker_holds_its_session(v1):
    """C-30.4 (review M1): a worker the cockpit started by selecting a session
    holds it while its pid in `S/native-workers.json` is alive, even with every
    outbox message finished: no history is bound there, and a bound
    conversation is fenced. A worker whose pid is gone holds nothing."""
    worker = _sleeper()
    workers = v1["state"] / "native-workers.json"
    try:
        write_json(workers, {f"claude:{LEGACY_SESSION}": {"pid": worker.pid, "broker_pid": DEAD_PID,
                                                          "account": ENROLLED}})
        report = run_legacy(v1)
        reason = f"the cockpit's worker pid {worker.pid} is live in it"
        items = {item["message_id"]: item for item in report.stores["outbox"].items if item["source"] == "outbox"}
        assert {key: (item["disposition"], item.get("detail")) for key, item in items.items()} == {
            LEGACY[0]: ("session-held-by-legacy-owner", reason), LEGACY[1]: ("session-held-by-legacy-owner", reason),
            LEGACY[2]: ("legacy-owned", None)}
        assert not conversations(v1["root"], "SELECT * FROM conversations")
    finally:
        worker.kill()
        worker.wait()
    assert dispositions(run_legacy(v1))[LEGACY[0]] == "history"      # the worker is gone
    (cid,) = [row["conversation_id"] for row in conversations(v1["root"], "SELECT * FROM conversations")]
    worker = _sleeper()
    try:
        write_json(workers, {f"claude:{LEGACY_SESSION.upper()}": {"pid": worker.pid}})
        report = run_legacy(v1)
        assert fences(report) == [bound(cid, "bound-session-held",
                                        f"the cockpit's worker pid {worker.pid} is live in it")]
    finally:
        worker.kill()
        worker.wait()


def test_an_unreadable_workers_file_holds_every_session(v1):
    """C-30.4 (review M1): a workers file that cannot be read could name any
    session, so it holds every one."""
    cid = _bind(v1)
    (v1["state"] / "native-workers.json").write_text("{torn", encoding="utf-8")
    report = run_legacy(v1)
    assert fences(report) == [bound(cid, "bound-session-held", "native-workers.json could not be read "
                                    "(JSONDecodeError), so a cockpit worker may be live in any session")]


@pytest.mark.parametrize("subfleet", [False, True])
def test_a_live_claude_process_outside_subfleet_keeps_history_out_and_fences_nothing(v1, subfleet):
    """C-30.4, C-26.3 (review M1 and its follow-up): a live Claude process
    registered for a session in `<claude dir>/sessions` keeps this pass from
    placing history there, unless it is Subfleet's own (its environment names
    `SUBFLEET_ATTEMPT`, as a turn that kept running across the restart does).
    It holds no conversation: admission makes a turn there wait while the
    process lives (`external-writer`), and a hold set now would outlast it."""
    cid = _bind(v1)
    write_outbox(v1["state"], [*fixture_rows(),
                               outbox_row(LEGACY[3], LEGACY_SESSION, "finished", "later, in the app", at=50)])
    process = _sleeper(**({"SUBFLEET_ATTEMPT": "turn-x/a1"} if subfleet else {}))
    try:
        write_json(v1["claude"] / "sessions" / f"{process.pid}.json",
                   {"sessionId": LEGACY_SESSION, "pid": process.pid, "cwd": str(v1["workspace"])})
        report = run_legacy(v1)
    finally:
        process.kill()
        process.wait()
    assert fences(report) == [] and _hold_of(v1, cid) is None
    if subfleet:
        assert dispositions(report)[LEGACY[3]] == "history"
    else:
        items = {item["message_id"]: item for item in report.stores["outbox"].items if item["source"] == "outbox"}
        assert (items[LEGACY[3]]["disposition"], items[LEGACY[3]]["detail"]) == (
            "session-held-by-legacy-owner", f"a live Claude process outside Subfleet (pid {process.pid}) holds it")
        assert any("waits at admission while it lives" in note for note in report.stores["outbox"].notes)
        assert dispositions(run_legacy(v1))[LEGACY[3]] == "history"      # it has ended


def test_a_conversation_subfleet_started_or_a_codex_thread_is_held_too(v1):
    """C-30.4, D-17 (review M4): every conversation bound to a held session is
    held, whatever its origin: one a Subfleet turn bound, and a Codex
    conversation on a thread the cockpit is continuing (`codex:<home>:<id>`)."""
    thread = "0199a5f3-0000-7000-8000-00000000c0de"
    v1["root"].mkdir(parents=True, exist_ok=True)
    store = ConversationStore(v1["root"])
    try:
        started, _ = store.create_conversation(provider="claude", workspace=str(v1["workspace"]),
                                               workspace_kind="in-place", settings=PERSON_SETTINGS, origin="new",
                                               request_id="r-1")
        store.update_conversation(started["conversation_id"], native_session_id=QUEUED_SESSION)
        codex, _ = store.create_conversation(provider="codex", workspace=str(v1["workspace"]),
                                             workspace_kind="in-place",
                                             settings={"model": "gpt-6-astra", "permission": "read-only"},
                                             origin="native", native_session_id=thread, lane_id="codex-1")
    finally:
        store.close()
    write_outbox(v1["state"], [*fixture_rows(), outbox_row(LEGACY[3], f"lane-codex-1:{thread.upper()}",
                                                           "dispatched", "a codex turn", provider="codex", at=50)])
    report = run_legacy(v1)
    by_id = {item["conversation_id"]: item for item in fences(report)}
    assert by_id[started["conversation_id"]]["legacy_hold"] == f"message {LEGACY[2]} is queued"
    assert by_id[codex["conversation_id"]]["legacy_hold"] == f"message {LEGACY[3]} is dispatched"
    assert by_id[codex["conversation_id"]]["session_id"] == f"codex:{thread}"
    assert dispositions(report)[LEGACY[3]] == "legacy-owned"


def test_one_session_has_one_spelling(v1):
    """C-24.1, C-30.4 (review L1): the v1 CLI stores a session id as typed. A
    finished message under the upper-case spelling of a session the cockpit is
    still using under the lower-case one is held with it, and nothing is bound;
    once it settles, history lands in one conversation bound to the lower-case
    id, which a lookup in either spelling finds."""
    done, busy = "0e0e0e0e-0000-4000-8000-000000000001", "0e0e0e0e-0000-4000-8000-000000000002"
    write_outbox(v1["state"], [outbox_row(done, LEGACY_SESSION.upper(), "finished", "via the CLI", at=600),
                               outbox_row(busy, LEGACY_SESSION, "dispatched", "the app, still running", at=60)])
    report = run_legacy(v1)
    assert dispositions(report) == {done: "session-held-by-legacy-owner", busy: "legacy-owned"}
    assert not conversations(v1["root"], "SELECT * FROM conversations")
    write_outbox(v1["state"], [outbox_row(done, LEGACY_SESSION.upper(), "finished", "via the CLI", at=600),
                               outbox_row(busy, LEGACY_SESSION, "finished", "the app, still running", at=60)])
    report = run_legacy(v1)
    assert dispositions(report) == {done: "history", busy: "history"}
    (row,) = conversations(v1["root"], "SELECT * FROM conversations")
    assert row["native_session_id"] == LEGACY_SESSION
    store = ConversationStore(v1["root"])
    try:
        assert store.by_native("claude", LEGACY_SESSION.upper())["conversation_id"] == row["conversation_id"]
        again, created = store.create_conversation(
            provider="claude", workspace=str(v1["workspace"]), workspace_kind="in-place",
            settings=PERSON_SETTINGS, origin="native", native_session_id=LEGACY_SESSION.upper())
        assert not created and again["conversation_id"] == row["conversation_id"]
    finally:
        store.close()


def test_a_session_bound_under_an_upper_case_id_before_is_still_found_and_fenced(v1):
    """C-24.1, C-30.4 (review L1): a store written before ids were canonical may
    bind a session under its upper-case spelling; the import still finds that
    conversation for the session's history and fences it."""
    v1["root"].mkdir(parents=True, exist_ok=True)
    store = ConversationStore(v1["root"])
    try:
        made, _ = store.create_conversation(provider="claude", workspace=str(v1["workspace"]),
                                            workspace_kind="in-place", settings=PERSON_SETTINGS, origin="legacy",
                                            native_session_id=LEGACY_SESSION)
        store.query("UPDATE conversations SET native_session_id=? WHERE conversation_id=?",
                    (LEGACY_SESSION.upper(), made["conversation_id"]))
    finally:
        store.close()
    report = run_legacy(v1)
    assert dispositions(report)[LEGACY[0]] == "history"
    assert len(conversations(v1["root"], "SELECT * FROM conversations")) == 1
    write_outbox(v1["state"], [*fixture_rows(),
                               outbox_row(LEGACY[3], LEGACY_SESSION, "starting", "the cockpit again", at=50)])
    report = run_legacy(v1)
    assert fences(report) == [bound(made["conversation_id"], "bound-session-held", f"message {LEGACY[3]} is starting")]


def test_the_fence_runs_before_any_history_is_placed(v1, monkeypatch):
    """C-30.4 (review L2): the fence needs only what the pass read, so it runs
    first, and a failure placing a session's history cannot leave a session the
    cockpit took up again unfenced."""
    cid = _bind(v1)
    other = "5e551011-0000-4000-8000-00000000000d"
    write_transcript(v1["claude"], other, v1["workspace"])
    write_outbox(v1["state"], [*fixture_rows(),
                               outbox_row(LEGACY[3], other, "finished", "another session", at=250),
                               outbox_row(LEGACY[4], LEGACY_SESSION, "queued", "the cockpit again", at=50)])

    def broken(*args, **kwargs):
        raise RuntimeError("the disk went away")
    monkeypatch.setattr(ConversationStore, "insert_legacy_history", broken)
    with pytest.raises(RuntimeError):
        run_legacy(v1)
    assert _hold_of(v1, cid) == f"message {LEGACY[4]} is queued"


def test_one_session_that_cannot_be_read_never_ends_the_pass(v1, monkeypatch):
    """C-30.4 (review L2): a transcript that raises while it is read, or a row
    whose timestamp is out of range, is reported with its disposition and the
    pass goes on to every other message and the fence."""
    other, third = "5e551011-0000-4000-8000-00000000000d", "5e551011-0000-4000-8000-00000000000e"
    for session in (other, third):
        write_transcript(v1["claude"], session, v1["workspace"])
    far = outbox_row(LEGACY[4], third, "finished", "from the far future", at=300)
    far = (*far[:6], 1e20, 1e20, far[8])
    write_outbox(v1["state"], [*fixture_rows(), outbox_row(LEGACY[3], other, "finished", "unreadable", at=250),
                               far])
    real = legacy.claude_session

    def reading(path):
        if other in str(path):
            raise AttributeError("'list' object has no attribute 'get'")
        return real(path)
    monkeypatch.setattr(legacy, "claude_session", reading)
    report = run_legacy(v1)
    items = {item["message_id"]: item for item in report.stores["outbox"].items if item["source"] == "outbox"}
    assert (items[LEGACY[3]]["disposition"], items[LEGACY[3]]["detail"]) == (
        "transcript-unreadable", "reading its transcript raised AttributeError")
    assert (items[LEGACY[4]]["disposition"], items[LEGACY[4]]["detail"]) == (
        "unreadable-row", "a timestamp is out of range")
    assert items[LEGACY[0]]["disposition"] == items[LEGACY[1]]["disposition"] == "history"
    monkeypatch.setattr(legacy, "claude_session", real)
    assert dispositions(run_legacy(v1))[LEGACY[3]] == "history"       # a later pass places it


def test_a_bound_session_the_legacy_writer_takes_up_again_waits_until_it_settles(v1, capsys):
    """C-30.4, C-24.5, design D-17: a session an earlier pass bound whose outbox
    gains a message that is not terminal is held again. Its conversation is held
    (`legacy_hold`), so no turn runs in it, and reported by id with its unsettled
    messages; a repeated pass changes nothing; the pass that finds the session
    settled lifts the hold."""
    run_legacy(v1)
    (conversation,) = conversations(v1["root"], "SELECT * FROM conversations")
    cid = conversation["conversation_id"]
    write_outbox(v1["state"], [*fixture_rows(),
                               outbox_row(LEGACY[3], LEGACY_SESSION, "dispatched", "the cockpit again", at=50)])
    report = run_legacy(v1)
    assert dispositions(report) == {LEGACY[0]: "already-imported", LEGACY[1]: "already-imported",
                                    LEGACY[2]: "legacy-owned", LEGACY[3]: "legacy-owned"}
    held = f"message {LEGACY[3]} is dispatched"
    assert fences(report) == [bound(cid, "bound-session-held", held)]
    outbox = report.stores["outbox"]
    assert outbox.reasons["bound-session-held"] == 1
    assert outbox.seen == 4 and outbox.skipped == 4           # a conversation is not one of the messages
    assert any("held while the legacy writer" in note for note in outbox.notes)

    # C-24.5: a person's next message waits; nothing in the session is dispatchable.
    person = "0f0f0f0f-0000-4000-8000-000000000001"
    store = ConversationStore(v1["root"])
    try:
        assert store.conversation(cid)["legacy_hold"] == held
        assert store.conversation(cid)["blocked_by"] is None
        store.submit_message(conversation_id=cid, message_id=person, after_message_id=None,
                             text="a person's next message", attachments=[], settings=PERSON_SETTINGS)
        assert store.next_dispatchable() == []
    finally:
        store.close()

    before = conversations(v1["root"], "SELECT * FROM conversations")
    capsys.readouterr()
    argv = ["--legacy-cockpit", "--state-root", str(v1["root"]), "--v1-state", str(v1["state"]),
            "--claude-dir", str(v1["claude"])]
    assert importer.main(argv) == 0
    table = capsys.readouterr().out
    assert (f"outbox: conversation {cid} claude:{LEGACY_SESSION} -> bound-session-held "
            f"{held}; unsettled: {person} queued") in table
    assert "the cockpit again" not in table and "a person's next message" not in table
    assert conversations(v1["root"], "SELECT * FROM conversations") == before

    # The cockpit's message finishes: the hold lifts, and the person's message is next.
    write_outbox(v1["state"], [*fixture_rows(),
                               outbox_row(LEGACY[3], LEGACY_SESSION, "finished", "the cockpit again", at=50)])
    report = run_legacy(v1)
    assert dispositions(report)[LEGACY[3]] == "conversation-has-own-messages"   # C-24.2: history goes first
    assert fences(report) == [bound(cid, "bound-session-released", None, unsettled=[(person, "queued")])]
    assert report.stores["outbox"].reasons["bound-session-released"] == 1
    store = ConversationStore(v1["root"])
    try:
        assert store.conversation(cid)["legacy_hold"] is None
        assert [message["message_id"] for message in store.next_dispatchable()] == [person]
    finally:
        store.close()


def test_a_legacy_hold_and_another_block_are_independent(v1):
    """C-30.4, C-24.8: the legacy hold is its own column. A bound conversation
    already blocked for another reason is held beside that block and reported
    with it; the pass that finds the session settled lifts only its hold, and
    the other block waits for its own resolution."""
    run_legacy(v1)
    (cid,) = [row["conversation_id"] for row in conversations(v1["root"], "SELECT * FROM conversations")]
    store = ConversationStore(v1["root"])
    try:
        store.update_conversation(cid, blocked_by="unfinished-turn")
    finally:
        store.close()
    write_outbox(v1["state"], [*fixture_rows(),
                               outbox_row(LEGACY[3], LEGACY_SESSION, "delivery-unknown", "ambiguous", at=50)])
    report = run_legacy(v1)
    held = f"message {LEGACY[3]} is delivery-unknown"
    assert fences(report) == [bound(cid, "bound-session-held", held, blocked_by="unfinished-turn")]
    assert conversations(v1["root"], "SELECT blocked_by, legacy_hold FROM conversations") == [
        {"blocked_by": "unfinished-turn", "legacy_hold": held}]
    write_outbox(v1["state"], [*fixture_rows(),
                               outbox_row(LEGACY[3], LEGACY_SESSION, "cancelled", "ambiguous", at=50, receipt={
                                   "ok": True, "error": None, "message": "Marked handled by you; no replay.",
                                   "resolution": "handled"})])
    report = run_legacy(v1)
    assert fences(report) == [bound(cid, "bound-session-released", None, blocked_by="unfinished-turn")]
    assert conversations(v1["root"], "SELECT blocked_by, legacy_hold FROM conversations") == [
        {"blocked_by": "unfinished-turn", "legacy_hold": None}]


def test_history_lands_in_the_sessions_one_conversation_whatever_its_origin(v1):
    """C-30.4, C-24.1: a session has at most one conversation, so a session that
    already has one with no message of its own gets its history there, keeping
    that conversation's origin, and the import creates none. A held session's
    conversation is held whatever its origin (review M4): one `conversation.open`
    made is held as one the import made would be."""
    v1["root"].mkdir(parents=True, exist_ok=True)
    store = ConversationStore(v1["root"])
    try:
        opened, _ = store.create_conversation(
            provider="claude", workspace=str(v1["workspace"]), workspace_kind="in-place",
            settings=PERSON_SETTINGS, origin="native", native_session_id=LEGACY_SESSION)
        queued, _ = store.create_conversation(
            provider="claude", workspace=str(v1["workspace"]), workspace_kind="in-place",
            settings=PERSON_SETTINGS, origin="native", native_session_id=QUEUED_SESSION)
    finally:
        store.close()
    report = run_legacy(v1)
    assert dispositions(report) == {LEGACY[0]: "history", LEGACY[1]: "history", LEGACY[2]: "legacy-owned"}
    assert "legacy-conversations-created" not in report.stores["outbox"].reasons
    queued_hold = f"message {LEGACY[2]} is queued"
    assert fences(report) == [bound(queued["conversation_id"], "bound-session-held", queued_hold,
                                    session=QUEUED_SESSION)]
    found = {row["conversation_id"]: row for row in conversations(v1["root"], "SELECT * FROM conversations")}
    assert set(found) == {opened["conversation_id"], queued["conversation_id"]}
    assert found[opened["conversation_id"]]["origin"] == "native"
    assert found[queued["conversation_id"]]["legacy_hold"] == queued_hold
    assert [(row["message_id"], row["origin"]) for row in conversations(
        v1["root"], "SELECT * FROM messages WHERE conversation_id=? ORDER BY seq", (opened["conversation_id"],))] == [
        (LEGACY[0], "legacy"), (LEGACY[1], "legacy")]

    write_outbox(v1["state"], [*fixture_rows(),
                               outbox_row(LEGACY[3], LEGACY_SESSION, "starting", "the cockpit again", at=50)])
    report = run_legacy(v1)
    by_id = sorted(fences(report), key=lambda item: item["conversation_id"])  # made in one millisecond
    assert by_id == sorted([bound(opened["conversation_id"], "bound-session-held", f"message {LEGACY[3]} is starting"),
                            bound(queued["conversation_id"], "bound-session-held", queued_hold,
                                  session=QUEUED_SESSION)], key=lambda item: item["conversation_id"])


def test_an_unreadable_row_is_reported_and_the_pass_goes_on(v1):
    """C-30.4: every message appears in the report with its disposition. A
    terminal row whose id is not a UUID in its canonical form, or whose payload
    has no prompt, is `unreadable-row`; the pass does not stop, and imports the
    rest. An upper-case UUID is the same id (`canonical_uuid` lowercases it) and
    is stored in its canonical form."""
    no_prompt = list(outbox_row(LEGACY[1], LEGACY_SESSION, "finished", "x", at=250))
    no_prompt[4] = "{}"
    not_json = list(outbox_row(LEGACY[2], LEGACY_SESSION, "error", "x", at=200))
    not_json[4] = "not json"
    bare = LEGACY[4].replace("-", "")                      # a UUID's hex, but not its canonical form
    write_outbox(v1["state"], [
        outbox_row(LEGACY[0], LEGACY_SESSION, "finished", "he replied", at=300), tuple(no_prompt), tuple(not_json),
        outbox_row(LEGACY[3].upper(), LEGACY_SESSION, "finished", "shouting", at=160),
        outbox_row(bare, LEGACY_SESSION, "finished", "x", at=150),
        outbox_row("m-finished", LEGACY_SESSION, "cancelled", "x", at=100)])
    report = run_legacy(v1)
    found = {item["message_id"]: (item["disposition"], item.get("detail")) for item in report.stores["outbox"].items}
    assert found == {
        LEGACY[0]: ("history", None),
        LEGACY[1]: ("unreadable-row", "the payload has no prompt"),
        LEGACY[2]: ("unreadable-row", "the payload has no prompt"),
        LEGACY[3].upper(): ("history", None),
        bare: ("unreadable-row", "the message id is not a canonical UUID"),
        "m-finished": ("unreadable-row", "the message id is not a canonical UUID")}
    assert report.stores["outbox"].reasons["unreadable-row"] == 4
    assert [row["message_id"] for row in conversations(v1["root"], "SELECT * FROM messages ORDER BY seq")] == [
        LEGACY[0], LEGACY[3]]


def test_image_snapshots_stay_in_v1_and_are_counted(v1):
    """C-30.4, migration.md `outbox` row: image snapshots stay in v1. The history
    row carries no attachment, its report item counts the images left behind, and
    `S/outbox-attachments/` belongs to the row, so it is not reported as unmanifested."""
    folder = v1["state"] / "outbox-attachments" / "message-x"
    folder.mkdir(parents=True)
    (folder / "image-0.png").write_bytes(b"png")
    write_outbox(v1["state"], [
        outbox_row(LEGACY[0], LEGACY_SESSION, "finished", "with pictures", at=300, images=2),
        outbox_row(LEGACY[1], LEGACY_SESSION, "finished", "without", at=200)])
    report = run_legacy(v1)
    items = {item["message_id"]: item for item in report.stores["outbox"].items}
    assert items[LEGACY[0]]["disposition"] == "history" and items[LEGACY[0]]["images_left_in_v1"] == 2
    assert "images_left_in_v1" not in items[LEGACY[1]]
    assert [json.loads(row["attachments_json"]) for row in conversations(
        v1["root"], "SELECT attachments_json FROM messages ORDER BY seq")] == [[], []]
    assert "outbox-attachments" not in report.unmanifested
    assert (folder / "image-0.png").read_bytes() == b"png"


def test_a_legacy_id_the_store_holds_as_a_message_of_its_own_is_a_conflict(v1):
    """C-30.4, C-24.2: a legacy id the store already holds as another origin's
    message is reported `message-id-conflict` with that message's conversation,
    and neither message changes; the rest of the session imports."""
    other = "0e0e0e0e-0000-4000-8000-000000000001"
    v1["root"].mkdir(parents=True, exist_ok=True)
    store = ConversationStore(v1["root"])
    try:
        mine, _ = store.create_conversation(
            provider="claude", workspace=str(v1["workspace"]), workspace_kind="in-place",
            settings=PERSON_SETTINGS, origin="native", native_session_id=other)
        store.submit_message(conversation_id=mine["conversation_id"], message_id=LEGACY[0], after_message_id=None,
                             text="the same id, typed here", attachments=[], settings=PERSON_SETTINGS)
    finally:
        store.close()
    report = run_legacy(v1)
    items = {item["message_id"]: item for item in report.stores["outbox"].items}
    assert items[LEGACY[0]]["disposition"] == "message-id-conflict"
    assert items[LEGACY[0]]["conversation_id"] == mine["conversation_id"]
    assert items[LEGACY[0]]["detail"] == "the store holds this id as a message of its own"
    assert items[LEGACY[1]]["disposition"] == "history" and items[LEGACY[1]]["seq"] == 1
    placed = {row["message_id"]: (row["conversation_id"], row["origin"])
              for row in conversations(v1["root"], "SELECT * FROM messages")}
    assert placed[LEGACY[0]] == (mine["conversation_id"], "person")
    assert placed[LEGACY[1]][0] != mine["conversation_id"] and placed[LEGACY[1]][1] == "legacy"


def test_a_conversation_store_newer_than_this_build_refuses_the_import(v1, capsys):
    """C-3.5, C-30.4: `--legacy-cockpit` refuses a `conversations.sqlite3` whose
    schema is newer than this build, exit 1 naming both versions, and writes
    nothing to it."""
    v1["root"].mkdir(parents=True, exist_ok=True)
    ConversationStore(v1["root"]).close()
    connection = sqlite3.connect(v1["root"] / "conversations.sqlite3")
    connection.execute("INSERT INTO schema_version VALUES (99, '2099-01-01T00:00:00Z')")
    connection.commit()
    connection.close()
    capsys.readouterr()
    assert importer.main(["--legacy-cockpit", "--state-root", str(v1["root"]), "--v1-state", str(v1["state"]),
                          "--claude-dir", str(v1["claude"])]) == 1
    err = capsys.readouterr().err
    assert "subfleet import: schema:" in err and "schema 99" in err
    assert not conversations(v1["root"], "SELECT * FROM conversations")


def test_notices_an_earlier_pass_made_from_the_outbox_are_left_alone(v1):
    """C-30.4: the six acknowledged notices the old mapping wrote stay as they are."""
    run_import(v1)
    with Store(v1["root"] / "state.sqlite3") as store:
        store._insert("notices", {"job_id": None, "session_id": f"claude:{LEGACY_SESSION}", "text": "he replied",
                                  "state": "acknowledged", "transport": "v1-socket",
                                  "created_at": "2026-08-31T19:34:55Z"}, kind="notice.imported")
    before = rows(v1["root"], "SELECT * FROM notices WHERE transport='v1-socket'")
    report = run_legacy(v1)
    assert rows(v1["root"], "SELECT * FROM notices WHERE transport='v1-socket'") == before
    assert any("left as they are" in note for note in report.stores["outbox"].notes)


def test_the_legacy_cockpit_runs_alone_and_its_dry_run_writes_nothing(v1, capsys):
    """C-30.4: `python -m subfleet.importer --legacy-cockpit [--dry-run]` touches no
    `state.sqlite3`; the dry run writes nothing under the state root but its report."""
    argv = ["--legacy-cockpit", "--state-root", str(v1["root"]), "--v1-state", str(v1["state"]),
            "--claude-dir", str(v1["claude"])]
    assert importer.main([*argv, "--dry-run", "--json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True and set(dry["stores"]) == {"outbox", "cockpit"}
    assert {item["message_id"]: item["disposition"] for item in dry["stores"]["outbox"]["items"]} == {
        LEGACY[0]: "history", LEGACY[1]: "history", LEGACY[2]: "legacy-owned"}
    names = sorted(path.name for path in v1["root"].iterdir())
    assert len(names) == 1 and names[0].startswith("import-report-"), names

    assert importer.main(argv) == 0
    table = capsys.readouterr().out
    assert f"{LEGACY[0]} claude:{LEGACY_SESSION} finished -> history (complete)" in table
    assert "he replied" not in table
    assert not (v1["root"] / "state.sqlite3").exists()
    assert len(conversations(v1["root"], "SELECT * FROM messages")) == 2

    report = import_legacy_cockpit(v1["root"], v1_state=v1["state"], claude_projects=v1["claude"] / "projects",
                                   dry_run=True, write_report=False)
    assert report.stores["outbox"].imported == 0      # the copy already holds both


def test_a_real_legacy_import_refuses_while_a_daemon_holds_the_lock(v1):
    """C-30.4, plan amendment 3, design D-4: a real `--legacy-cockpit` run is
    refused while a daemon holds `daemon.lock`, since the daemon is
    conversations.sqlite3's writer; a dry run is not."""
    root = v1["root"]
    root.mkdir(parents=True, exist_ok=True)
    handle = os.open(root / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ImportRefused):
            import_legacy_cockpit(root, v1_state=v1["state"], claude_projects=v1["claude"] / "projects")
        assert importer.main(["--legacy-cockpit", "--state-root", str(root), "--v1-state", str(v1["state"]),
                              "--claude-dir", str(v1["claude"])]) == 7
        import_legacy_cockpit(root, v1_state=v1["state"], claude_projects=v1["claude"] / "projects",
                              dry_run=True)
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
    assert not (root / "conversations.sqlite3").exists()


def _lock_probe(root: Path, held: list[bool]):
    """Record whether another process could take `daemon.lock` right now."""
    def probe() -> None:
        handle = os.open(root / "daemon.lock", os.O_RDWR)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
            held.append(False)
        except BlockingIOError:
            held.append(True)
        finally:
            os.close(handle)
    return probe


def test_the_legacy_import_holds_the_lock_for_its_whole_pass(v1, monkeypatch):
    """C-30.4: a real `--legacy-cockpit` run holds `daemon.lock` from before its
    first row to after its last, so no daemon can start mid-pass."""
    held: list[bool] = []
    probe = _lock_probe(v1["root"], held)
    first, last = importer.import_outbox, importer.import_cockpit_client
    monkeypatch.setattr(importer, "import_outbox", lambda *a, **k: (probe(), first(*a, **k))[1])
    monkeypatch.setattr(importer, "import_cockpit_client", lambda *a, **k: (last(*a, **k), probe())[0])
    report = import_legacy_cockpit(v1["root"], v1_state=v1["state"], claude_projects=v1["claude"] / "projects",
                                   write_report=False)
    assert held == [True, True]
    assert report.stores["outbox"].imported == 2
    probe()
    assert held[-1] is False                                  # and lets it go when the pass ends


# --- the salt -----------------------------------------------------------------

def test_the_salt_is_copied_so_event_ids_stay_stable(v1):
    """Manifest row `S/integration-events.salt`."""
    run_import(v1)
    copied = v1["root"] / "integration-events.salt"
    assert copied.read_bytes() == (v1["state"] / "integration-events.salt").read_bytes()
    assert copied.stat().st_mode & 0o777 == 0o600


def test_a_different_salt_at_the_destination_is_never_overwritten(v1):
    root = v1["root"]
    root.mkdir(parents=True, exist_ok=True)
    (root / "integration-events.salt").write_bytes(b"a different salt entirely-------")
    report = run_import(v1)
    assert report.stores["salt"].reasons.get("destination-differs") == 1
    assert (root / "integration-events.salt").read_bytes() == b"a different salt entirely-------"


# --- staged rows --------------------------------------------------------------

def test_rows_staged_for_a_later_milestone_are_not_imported(v1):
    """The manifest imports alerts at milestone 5 and the sessions kit at 6."""
    report = run_import(v1)
    assert report.stores["alerts"].disposition == "staged-milestone-5"
    assert report.stores["sessions-kit"].disposition == "retain-until-milestone-6"
    assert report.stores["gates"].disposition == "retain-until-milestone-7"
    assert not rows(v1["root"], "SELECT * FROM events WHERE kind IN "
                                "('alert-latch','tickle','native-worker')")


def test_alerts_import_as_latches_at_milestone_five(v1):
    """Manifest row `S/alerts.json`: events of kind `alert-latch` (C-18.1)."""
    run_import(v1, milestone=5)
    latches = rows(v1["root"], "SELECT * FROM events WHERE kind='alert-latch'")
    assert len(latches) == 2
    assert {json.loads(row["data_json"])["source"] for row in latches} == {"v1-alerts"}
    run_import(v1, milestone=5)
    assert len(rows(v1["root"], "SELECT * FROM events WHERE kind='alert-latch'")) == 2


def test_the_sessions_kit_imports_at_milestone_six(v1):
    """Manifest row `S/tickles/`, `S/native-workers.json`: tickle and native-worker events."""
    run_import(v1, milestone=6)
    tickles = rows(v1["root"], "SELECT * FROM events WHERE kind='tickle'")
    workers = rows(v1["root"], "SELECT * FROM events WHERE kind='native-worker'")
    assert len(tickles) == 1 and json.loads(tickles[0]["data_json"])["session_id"] == SESSION
    assert len(workers) == 1
    assert not rows(v1["root"], "SELECT * FROM events WHERE kind='revive'")


# --- what the manifest does not name ------------------------------------------

def test_an_unknown_store_is_reported_and_untouched(v1):
    """migration.md: anything not in the table is reported and left alone."""
    mystery = v1["state"] / "mystery-store.json"
    before = mystery.read_bytes()
    report = run_import(v1)
    assert "mystery-store.json" in report.unmanifested
    assert "snapshot.json" not in report.unmanifested       # a manifest `drop` row
    assert mystery.read_bytes() == before


def test_dropped_stores_reach_nothing_in_the_store(v1):
    """migration.md `drop` rows: derived caches and the statusline tap are dead.

    `history.jsonl` and `lane-usage.jsonl` in particular are "never imported as
    readings or percentages" (principle 2).
    """
    (v1["state"] / "history.jsonl").write_text(
        json.dumps({"email": ENROLLED, "used_percent": 91}) + "\n", encoding="utf-8")
    run_import(v1)
    payload = json.dumps(rows(v1["root"], "SELECT * FROM readings")
                         + rows(v1["root"], "SELECT * FROM closures"))
    assert "history.jsonl" not in payload and "snapshot.json" not in payload
    assert not rows(v1["root"], "SELECT * FROM readings WHERE utilization=0.91")


# --- the dry run --------------------------------------------------------------

def test_a_dry_run_writes_no_row_and_no_state_root_file_but_the_report(v1):
    """Brief: the dry run produces an ImportReport without writing to any store."""
    report = run_import(v1, dry_run=True)
    assert report.imported > 0
    assert report.dry_run is True
    root = v1["root"]
    assert not (root / "state.sqlite3").exists()
    assert not (root / "jobs").exists()
    assert not (root / "integration-events.salt").exists()
    assert Path(report.path).name.startswith("import-report-")
    assert sorted(path.name for path in root.iterdir()) == [Path(report.path).name]


def test_a_dry_run_over_an_existing_store_leaves_it_untouched(v1):
    run_import(v1)
    before = dump(v1["root"])
    digest = (v1["root"] / "state.sqlite3").stat().st_mtime_ns
    report = run_import(v1, dry_run=True)
    assert dump(v1["root"]) == before
    assert (v1["root"] / "state.sqlite3").stat().st_mtime_ns == digest
    assert report.imported == 0            # the snapshot already holds every row


def test_a_dry_run_counts_what_a_real_pass_would_write(v1):
    dry = run_import(v1, dry_run=True)
    real = run_import(v1)
    for key, entry in real.stores.items():
        assert dry.stores[key].imported == entry.imported, key


def test_the_report_names_every_manifest_row(v1):
    report = run_import(v1)
    assert set(report.stores) == {row.key for row in importer.MANIFEST}
    payload = json.loads(Path(report.path).read_text())
    assert payload["dry_run"] is False
    assert payload["stores"]["runs"]["imported"] == report.stores["runs"].imported


# --- preconditions ------------------------------------------------------------

def test_a_real_import_refuses_while_a_daemon_holds_the_lock(v1):
    """Plan amendment 3: the daemon owns the store's writes (C-3.4)."""
    root = v1["root"]
    root.mkdir(parents=True, exist_ok=True)
    handle = os.open(root / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ImportRefused):
            run_import(v1)
        run_import(v1, dry_run=True)          # a dry run never takes that path
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def test_the_import_holds_the_lock_for_its_whole_pass(v1):
    """A check that is released before the work leaves a window for two writers."""
    held: list[bool] = []

    def probe(*_args, **_kwargs) -> None:
        handle = os.open(v1["root"] / "daemon.lock", os.O_RDWR)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
            held.append(False)
        except BlockingIOError:
            held.append(True)
        finally:
            os.close(handle)

    original = importer.import_salt
    try:
        importer.import_salt = lambda *a, **k: (probe(), original(*a, **k))[1]
        run_import(v1)
    finally:
        importer.import_salt = original
    assert held == [True]


def test_an_unopenable_daemon_lock_refuses_rather_than_assuming_no_daemon(v1):
    """The importer never proceeds on the strength of a check it could not make."""
    root = v1["root"]
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "daemon.lock"
    lock.mkdir()                        # not a file: os.open for writing fails
    with pytest.raises(ImportRefused):
        run_import(v1)


def test_a_wal_v1_database_is_never_opened_in_place(v1):
    """Opening a WAL database read-only still writes its `-shm`; v1 is read-only."""
    outbox = v1["state"] / "outbox.sqlite3"
    connection = sqlite3.connect(outbox)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("INSERT INTO messages(message_id,session_id,request_digest,"
                       "payload_digest,payload,status,created_at,updated_at,receipt) "
                       "VALUES('m-wal','claude:x','d','p','{}','queued',1.0,1.0,'')")
    connection.commit()
    connection.close()
    # SQLite removed the sidecars when the writer closed, but the header still says
    # WAL, so a read-only open would recreate `-wal` and `-shm` right here.
    assert not outbox.with_name(outbox.name + "-shm").exists()
    before = sorted(path.name for path in v1["state"].iterdir())
    report = run_legacy(v1)
    assert sorted(path.name for path in v1["state"].iterdir()) == before
    assert report.stores["outbox"].seen == 4        # the WAL row was read all the same
    assert dispositions(report)["m-wal"] == "legacy-owned"      # C-30.4: queued, so still v1's


def test_nothing_under_the_v1_state_is_modified(v1):
    """Brief: nothing in this lane touches v1's files except to read them."""
    def snapshot() -> dict[str, tuple[int, int]]:
        found = {}
        for root in (v1["state"], v1["delegate"], v1["roster"], v1["claude"]):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    stat = path.stat()
                    found[str(path)] = (stat.st_size, stat.st_mtime_ns)
        return found

    before = snapshot()
    run_import(v1)
    run_import(v1, dry_run=True)
    # C-30.4, C-30.1: the legacy rows read the outbox, the journal and the Claude
    # transcripts, and write none of them.
    run_legacy(v1, dry_run=True)
    run_legacy(v1)
    import_legacy_cockpit(v1["root"], v1_state=v1["state"], claude_projects=v1["claude"] / "projects")
    assert snapshot() == before


def test_a_v1_state_that_is_not_there_releases_nothing(v1):
    """C-30.4 (review follow-up C): a `--v1-state` that is not a directory is
    not a cockpit that holds nothing. The legacy pass refuses it (exit 7), and
    a milestone pass reads nothing from it and leaves every hold as it is."""
    cid = _bind(v1)
    write_outbox(v1["state"], [*fixture_rows(),
                               outbox_row(LEGACY[3], LEGACY_SESSION, "dispatched", "the cockpit again", at=50)])
    run_legacy(v1)
    held = f"message {LEGACY[3]} is dispatched"
    assert _hold_of(v1, cid) == held
    missing = v1["state"].parent / "v1-stat"                      # a typo
    empty = v1["state"].parent / "empty"
    empty.mkdir()
    (v1["root"] / "gates").mkdir(exist_ok=True)                    # a v2 root can hold manifest names too
    other_v2 = v1["state"].parent / "other-v2-root"
    (other_v2 / "gates").mkdir(parents=True)
    (other_v2 / "state.sqlite3").write_bytes(b"")
    # not there, empty, the directory above, this v2 state root (--v1-state and --state-root swapped), another
    for wrong in (missing, empty, v1["state"].parent, v1["root"], other_v2):
        with pytest.raises(ImportRefused):
            import_legacy_cockpit(v1["root"], v1_state=wrong, claude_projects=v1["claude"] / "projects")
    assert importer.main(["--legacy-cockpit", "--state-root", str(v1["root"]), "--v1-state", str(missing),
                          "--claude-dir", str(v1["claude"])]) == 7
    report = import_v1(v1["root"], v1_state=missing, delegate_state=v1["delegate"], roster_dir=v1["roster"],
                       home=v1["home"], milestone=importer.LEGACY_MILESTONE, claude_projects=v1["claude"] / "projects")
    assert report.stores["outbox"].reasons == {"v1-state-missing": 1}
    assert _hold_of(v1, cid) == held


def test_a_retired_cockpit_lifts_every_hold(v1, capsys):
    """C-30.4: once the legacy cockpit will never run again, `--cockpit-retired`
    lifts every legacy hold and forgets every held session without reading the
    v1 state; its dry run changes nothing."""
    cid = _bind(v1)
    _unreadable_journal(v1["state"] / legacy.JOURNAL, "not json")
    run_legacy(v1)
    assert _hold_of(v1, cid) and _recorded(v1)
    gone = v1["state"].parent / "retired"
    argv = ["--legacy-cockpit", "--cockpit-retired", "--state-root", str(v1["root"]), "--v1-state", str(gone)]
    assert importer.main([*argv, "--dry-run"]) == 0
    assert _hold_of(v1, cid) and _recorded(v1)
    capsys.readouterr()
    assert importer.main(argv) == 0
    assert f"conversation {cid} claude:{LEGACY_SESSION} -> bound-session-released" in capsys.readouterr().out
    assert _hold_of(v1, cid) is None and _recorded(v1) == {}
    with pytest.raises(SystemExit):
        importer.main(["--cockpit-retired", "--state-root", str(v1["root"])])
    # Retirement lasts: the cockpit's unsettled rows never settle, and no later pass holds them again.
    (v1["state"] / legacy.JOURNAL).unlink()
    write_outbox(v1["state"], [*fixture_rows(),
                               outbox_row(LEGACY[3], LEGACY_SESSION, "dispatched", "never settles", at=50)])
    for report in (run_legacy(v1), import_legacy_cockpit(v1["root"], v1_state=v1["state"],
                                                          claude_projects=v1["claude"] / "projects")):
        assert report.stores["outbox"].reasons == {"cockpit-retired": 1}
    assert _hold_of(v1, cid) is None and _recorded(v1) == {}


def test_a_dangling_manifest_entry_still_marks_a_v1_state(v1):
    """C-30.4: a v1 state whose only manifest entry is a symlink to a moved
    file is still a v1 state, read as holding nothing in that entry."""
    lonely = v1["state"].parent / "lonely"
    lonely.mkdir()
    (lonely / "outbox.sqlite3").symlink_to(lonely / "moved-away.sqlite3")
    report = import_legacy_cockpit(v1["root"], v1_state=lonely, claude_projects=v1["claude"] / "projects",
                                   write_report=False)
    assert report.stores["outbox"].reasons == {"absent": 1}


def test_retirement_with_no_store_is_recorded_and_later_passes_read_nothing(v1):
    """C-30.4 (fourth review, findings 3 and 4): retiring before any conversation
    store exists creates it to record the retirement. Every later pass reads
    nothing from the cockpit, its journal included, and a legacy pass is not
    refused once the v1 state is gone."""
    assert importer.main(["--legacy-cockpit", "--cockpit-retired", "--state-root", str(v1["root"]),
                          "--v1-state", str(v1["state"])]) == 0
    write_json(v1["state"] / legacy.JOURNAL, {f"claude:{LEGACY_SESSION}": {"request": {
        "message_id": LEGACY[5], "session_id": f"claude:{LEGACY_SESSION}"}}})
    report = run_legacy(v1)
    assert report.stores["outbox"].reasons == {"cockpit-retired": 1}
    assert report.stores["cockpit"].reasons == {"cockpit-retired": 1} and report.stores["cockpit"].items == []
    assert _recorded(v1) == {} and not conversations(v1["root"], "SELECT * FROM conversations")
    report = import_legacy_cockpit(v1["root"], v1_state=v1["state"].parent / "gone",
                                   claude_projects=v1["claude"] / "projects")
    assert report.stores["outbox"].reasons == {"cockpit-retired": 1}


def test_a_retirement_that_dies_midway_retires_nothing(v1, monkeypatch):
    """C-30.4 (fourth review, finding 5): the retirement is recorded last, so a
    run that dies while lifting the holds leaves the cockpit read as before, and
    the next pass still fences and releases."""
    cid = _bind(v1)
    write_outbox(v1["state"], [*fixture_rows(),
                               outbox_row(LEGACY[3], LEGACY_SESSION, "dispatched", "the cockpit again", at=50)])
    run_legacy(v1)
    assert _hold_of(v1, cid)
    real = ConversationStore.set_legacy_hold

    def dying(self, conversation_id, reason):
        raise OSError("the disk went away")
    monkeypatch.setattr(ConversationStore, "set_legacy_hold", dying)
    with pytest.raises(OSError):
        import_legacy_cockpit(v1["root"], v1_state=v1["state"], cockpit_retired=True)
    monkeypatch.setattr(ConversationStore, "set_legacy_hold", real)
    write_outbox(v1["state"], fixture_rows())                          # the cockpit's message settles
    report = run_legacy(v1)
    assert "cockpit-retired" not in report.stores["outbox"].reasons
    assert fences(report) == [bound(cid, "bound-session-released", None)]
