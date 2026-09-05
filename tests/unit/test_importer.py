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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from subfleet import importer
from subfleet.importer import ImportRefused, import_v1
from subfleet.store import Store

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

    outbox = sqlite3.connect(state / "outbox.sqlite3")
    outbox.execute("""CREATE TABLE messages (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL,
        session_id TEXT NOT NULL, request_digest TEXT NOT NULL, payload_digest TEXT NOT NULL,
        payload TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
        updated_at REAL NOT NULL, receipt TEXT NOT NULL)""")
    stamp = datetime.now(timezone.utc).timestamp()
    outbox.executemany(
        "INSERT INTO messages(message_id,session_id,request_digest,payload_digest,payload,"
        "status,created_at,updated_at,receipt) VALUES(?,?,?,?,?,?,?,?,?)", [
            ("m-1", f"claude:{SESSION}", "d1", "p1",
             json.dumps({"prompt": "he replied", "session_id": f"claude:{SESSION}"}),
             "finished", stamp - 300, stamp - 200, json.dumps({"ok": True})),
            ("m-2", f"claude:{SESSION}", "d2", "p2",
             json.dumps({"prompt": "no receipt yet", "session_id": f"claude:{SESSION}"}),
             "finished", stamp - 100, stamp - 90, ""),
            ("m-3", f"claude:{SESSION}", "d3", "p3",
             json.dumps({"prompt": "queued", "session_id": f"claude:{SESSION}"}),
             "queued", stamp - 50, stamp - 50, ""),
        ])
    outbox.commit()
    outbox.close()

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
            "root": tmp_path / "v2", "runs": runs, "metas": metas}


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
    assert {"runs", "notices", "outbox", "capacity-live-cache", "claude-oauth-raw",
            "keepalive", "reset-policy", "cooldowns"} <= set(cursors)
    assert cursors["runs"]["last_id"] == "20260905-100600-nolane"
    assert cursors["notices"]["files"][f"{SESSION}.jsonl"]["lines"] == 4
    # The two entries whose runs are not in the ledger are kept for retry.
    assert cursors["notices"]["files"][f"{SESSION}.jsonl"]["retry"] == [2, 3]
    assert cursors["outbox"]["last_sequence"] == 3


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
        f"reset-credit:codex:{CODEX_ONE}:RateLimitResetCredit_26c531af84688191afcbab1b15c7ec69")
    without = [row for row in actions if row not in with_credit][0]
    assert json.loads(without["request_json"])["credit_id_absent_in_v1"] is True


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


# --- notices and the outbox ---------------------------------------------------

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


def test_outbox_rows_become_offered_or_acknowledged(v1):
    """Manifest row `S/outbox.sqlite3`: delivered plus a receipt is acknowledged,
    everything else is offered, transport `v1-socket` (C-15.3)."""
    run_import(v1)
    messages = rows(v1["root"], "SELECT * FROM notices WHERE transport='v1-socket' "
                                "ORDER BY notice_id")
    assert [row["state"] for row in messages] == ["acknowledged", "offered", "offered"]
    assert all(row["transport"] == "v1-socket" for row in messages)
    assert all(row["job_id"] is None for row in messages)
    assert all(row["session_id"] == f"claude:{SESSION}" for row in messages)


def test_an_unrecognised_outbox_status_is_reported_by_name(v1):
    report = run_import(v1)
    assert report.stores["outbox"].reasons.get("status-queued") == 1


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
    report = run_import(v1)
    assert sorted(path.name for path in v1["state"].iterdir()) == before
    assert report.stores["outbox"].seen == 4        # the WAL row was read all the same
    assert rows(v1["root"], "SELECT * FROM notices WHERE text='m-wal'")


def test_nothing_under_the_v1_state_is_modified(v1):
    """Brief: nothing in this lane touches v1's files except to read them."""
    def snapshot() -> dict[str, tuple[int, int]]:
        found = {}
        for root in (v1["state"], v1["delegate"], v1["roster"]):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    stat = path.stat()
                    found[str(path)] = (stat.st_size, stat.st_mtime_ns)
        return found

    before = snapshot()
    run_import(v1)
    run_import(v1, dry_run=True)
    assert snapshot() == before
