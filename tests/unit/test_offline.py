"""Offline mode against a temp store: runs, runs show, status, and kill (C-17.5).

Every test names the clause it proves (C-20.5).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from subfleet import cli, offline
from subfleet.client import boot_id
from subfleet.offline import Offline

SCHEMA = Path(__file__).resolve().parents[2] / "subfleet" / "store_schema.sql"
JOB = "20260905-120000-demo"
DONE = "20260905-110000-done"
NOW = "2026-09-05T12:00:00Z"


SAME_AS_GUARDIAN = object()


def build_store(root: Path, *, pgid=SAME_AS_GUARDIAN,
                guardian_pid: int | None = 999999,
                proc_start: str | None = "Mon Jan  1 00:00:00 2001") -> Path:
    """A store with one running job, one finished job, a lane, and a reading.

    The guardian leads its own process group (C-5.1), so pgid follows
    guardian_pid unless a test is deliberately recording a mismatch. The boot id
    is this machine's real one, so the identity checks mean what they say and
    the fixture does not expire at the next reboot (C-5.3).
    """
    if pgid is SAME_AS_GUARDIAN:
        pgid = guardian_pid
    root.mkdir(parents=True, exist_ok=True)
    path = root / "state.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.read_text())
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (1, ?)", (NOW,))
    conn.execute(
        "INSERT INTO lanes (lane_id, provider, account_key, credential_ref,"
        " credential_kind, home, owner, desktop, enabled, plan, created_at, updated_at)"
        " VALUES ('codex-1','codex','codex:acct-1','~/.codex-1','home','~/.codex-1',"
        f"'v2',0,1,'pro','{NOW}','{NOW}')")
    conn.execute(
        "INSERT INTO readings (lane_id, scope, window, utilization, resets_at, label,"
        f" source, observed_at) VALUES ('codex-1','account','five_hour',0.42,"
        f"'2026-09-05T18:00:00Z','provider','wham','{NOW}')")
    conn.execute(
        "INSERT INTO closures (lane_id, scope, until_at, reason, clock_source,"
        f" source_event, created_at) VALUES ('codex-1','gpt-6-astra',"
        f"'2026-09-05T18:00:00Z','provider-limit','reported','rc4','{NOW}')")
    deliverable = root / "out.md"
    deliverable.write_text("# the deliverable\n")
    stderr = root / "err.log"
    stderr.write_text("a warning\n")
    for job_id, state, rc, session in ((JOB, "running", None, "sess-1"),
                                       (DONE, "succeeded", 0, "sess-2")):
        conn.execute(
            "INSERT INTO jobs (job_id, request_id, payload_digest, kind, state, task,"
            " tier, workdir, prompt_path, out_path, sandbox, caller_session, name,"
            " max_attempts, max_wall_s, created_at, started_at, finished_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, f"rq-{job_id}", "sha", "dispatch", state, "build", "standard",
             "/Users/x/repo", str(root / "prompt.md"),
             str(deliverable) if rc == 0 else None, "read-only", session, "demo",
             3, 21600, NOW, NOW,
             "2026-09-05T12:05:00Z" if rc == 0 else None))
        conn.execute(
            "INSERT INTO attempts (attempt_id, job_id, seq, lane_id, model_requested,"
            " model_served, attestation, state, guardian_pid, child_pid, pgid, boot_id,"
            " proc_start, rc, outcome_class, reserved_at, started_at, finished_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{job_id}/a1", job_id, 1, "codex-1", "gpt-6-astra",
             "gpt-6-astra" if rc == 0 else None,
             "attested" if rc == 0 else "unattested",
             "succeeded" if rc == 0 else "running",
             guardian_pid if rc is None else 1234, 1235,
             pgid if rc is None else 4243, boot_id(), proc_start, rc,
             "ok" if rc == 0 else None, NOW, NOW,
             "2026-09-05T12:05:00Z" if rc == 0 else None))
    conn.execute(
        "INSERT INTO artifacts (attempt_id, role, path, sha256, bytes, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (f"{DONE}/a1", "deliverable", str(deliverable), "sha", 18, NOW))
    conn.execute(
        "INSERT INTO artifacts (attempt_id, role, path, sha256, bytes, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (f"{DONE}/a1", "stderr", str(stderr), "sha", 10, NOW))
    conn.execute(
        "INSERT INTO notices (job_id, session_id, text, state, created_at)"
        " VALUES (?,?,?,?,?)", (DONE, "sess-2", "done: rc 0", "pending", NOW))
    conn.execute(
        "INSERT INTO decisions (job_id, attempt_id, evaluated_at, policy_hash,"
        " decision_json) VALUES (?,?,?,?,?)",
        (DONE, f"{DONE}/a1", NOW, "policy-sha",
         json.dumps({"chain": ["opus", "astra"], "chosen_model": "astra",
                     "chosen_lane": "codex-1", "reason": "promoted"})))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def store(root):
    build_store(root)
    return root


# --- reads (C-17.5, C-3.4) ----------------------------------------------------

def test_runs_renders_from_the_store_when_the_daemon_is_down(store, capsys):
    """C-17.5 with no daemon, `runs` reads the store and renders the table."""
    assert cli.main(["runs"]) == 0
    captured = capsys.readouterr()
    assert JOB in captured.out and DONE in captured.out
    assert "RUNNING" in captured.out and "gpt-6-astra" in captured.out
    assert "offline" in captured.err


def test_runs_running_and_last_filter_offline(store, capsys):
    """C-17.5 `--running` and `--last` work offline."""
    assert cli.main(["runs", "--running"]) == 0
    captured = capsys.readouterr().out
    assert JOB in captured and DONE not in captured
    assert cli.main(["jobs", "--last", "1"]) == 0
    rows = [line for line in capsys.readouterr().out.splitlines() if JOB in line
            or DONE in line]
    assert len(rows) == 1


def test_runs_mine_filters_by_caller_session_offline(store, monkeypatch, capsys):
    """C-17.5 `runs --mine` filters on the caller session offline."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-2")
    assert cli.main(["runs", "--mine"]) == 0
    captured = capsys.readouterr().out
    assert DONE in captured and JOB not in captured


def test_runs_show_renders_metadata_artifacts_and_out_offline(store, capsys):
    """C-17.5 `runs show` and `--out`/`--err` work from the store and its files."""
    assert cli.main(["show", DONE]) == 0
    text = capsys.readouterr().out
    assert DONE in text and "deliverable" in text and "attested" in text
    assert "done: rc 0" in text
    assert cli.main(["runs", "show", DONE, "--out"]) == 0
    assert capsys.readouterr().out == "# the deliverable\n"
    assert cli.main(["runs", "show", DONE, "--err"]) == 0
    assert capsys.readouterr().out == "a warning\n"
    assert cli.main(["runs", "show", DONE, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["job_id"] == DONE and payload["offline"] is True
    assert payload["decision"]["chosen_model"] == "astra"


def test_status_renders_lanes_readings_and_closures_offline(store, capsys):
    """C-17.5 `status` renders lanes, readings, and live closures from the store."""
    assert cli.main(["status"]) == 0
    text = capsys.readouterr().out
    assert "codex-1" in text and "42%" in text
    assert "closures" in text and "gpt-6-astra" in text
    assert "running jobs: 1" in text


def test_show_of_an_unknown_job_is_invalid_input(store, capsys):
    """C-17.3 asking for a job the store does not have is exit 2."""
    assert cli.main(["runs", "show", "20260101-000000-nope"]) == 2
    assert "no job" in capsys.readouterr().err


def test_the_store_is_opened_read_only(store):
    """C-3.4 readers other than the daemon open the database read-only."""
    conn = Offline(store).connect()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE jobs SET state = 'failed'")
    conn.close()


def test_no_store_and_no_daemon_exits_69_with_the_fix(root, capsys):
    """C-17.5 nothing to read from is exit 69 naming `subfleet daemon start`."""
    assert cli.main(["runs"]) == 69
    captured = capsys.readouterr()
    assert "no daemon and no store" in captured.err
    assert "subfleet daemon start" in captured.err


def test_offline_mode_never_serves_the_verbs_that_need_a_daemon(root, capsys):
    """C-17.5 everything but runs/show/status/kill exits 69 offline."""
    build_store(root)
    for argv in (["why", JOB], ["lanes"], ["resume", JOB], ["ping", "--session", "s", "x"]):
        assert cli.main(argv) == 69, argv
        assert "subfleet daemon start" in capsys.readouterr().err


# --- kill (C-17.5, C-5.3, C-5.4) ---------------------------------------------

def test_offline_kill_refuses_when_identity_cannot_be_verified(root, capsys):
    """C-17.5, C-5.3 offline kill refuses a pid it cannot confirm and says why."""
    build_store(root, guardian_pid=os.getpid(), proc_start=None)
    assert cli.main(["kill", JOB]) == 1
    captured = capsys.readouterr()
    assert "refused" in captured.out
    assert "cannot verify" in captured.err and "C-5.3" in captured.err


def test_offline_kill_reports_a_gone_guardian_without_signalling(store, capsys):
    """C-5.3, C-5.4 a recorded pid that is provably gone is never signalled."""
    assert cli.main(["kill", JOB]) == 0
    captured = capsys.readouterr()
    assert "already-dead" in captured.out
    assert "the daemon will finalize it" in captured.err


def test_offline_kill_refuses_a_job_with_no_recorded_pgid(root, capsys):
    """C-5.4 the daemon may signal only a process group it recorded."""
    build_store(root, pgid=None, guardian_pid=None)
    assert cli.main(["kill", JOB]) == 1
    assert "no recorded pgid" in capsys.readouterr().err


def test_offline_kill_of_a_finished_job_is_already_finished(store, capsys):
    """C-7.2 a job that already finished reports so and exits 0."""
    assert cli.main(["kill", DONE]) == 0
    assert "already-finished" in capsys.readouterr().out


def test_offline_kill_signals_a_verified_process_group(root, capsys, monkeypatch):
    """C-5.4, C-17.5 a verified identity is the only thing offline kill signals."""
    signalled: list[tuple[int, int]] = []
    build_store(root, guardian_pid=os.getpid(), proc_start="recorded")
    monkeypatch.setattr("subfleet.offline.same_process", lambda *a, **k: True)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))
    assert cli.main(["kill", JOB]) == 0
    assert signalled == [(os.getpid(), 15)]
    captured = capsys.readouterr()
    assert "signalled" in captured.out
    assert "until a daemon reconciles it" in captured.err


def test_offline_kill_json_emits_one_object(store, capsys):
    """C-17.4 `--json` emits JSON only, offline as well."""
    assert cli.main(["kill", JOB, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["job_id"] == JOB and payload["action"] == "already-dead"


# --- reap ---------------------------------------------------------------------

def test_reap_names_the_orphans_without_writing(store, capsys, monkeypatch):
    """C-4.2, C-3.4 `runs reap` reports jobs whose runner is gone; the daemon writes."""
    from subfleet import procs
    monkeypatch.setattr(procs, "liveness", lambda *args: "dead")
    assert cli.main(["runs", "reap"]) == 0
    captured = capsys.readouterr()
    assert JOB in captured.out and "runner is gone" in captured.out
    assert ("identity checked with subfleet.procs" in captured.err
            or "identity checked with subfleet.client" in captured.err)
    assert cli.main(["runs", "reap", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["job_id"] == JOB and payload["pid"] == 999999


def test_kill_refuses_a_store_written_by_a_newer_subfleet(root, capsys):
    """C-3.5 a store at a newer schema version is refused with both versions."""
    path = build_store(root)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (99, ?)",
                 (NOW,))
    conn.commit()
    conn.close()
    assert cli.main(["kill", JOB]) == 1
    captured = capsys.readouterr()
    assert ("schema version 99" in captured.err
            and f"version {offline.KNOWN_SCHEMA_VERSION}" in captured.err)


def test_the_offline_reader_knows_the_schema_the_daemon_writes(root, capsys):
    """C-3.5, C-17.5 a store this build wrote is never "newer than this CLI".

    The two constants are one fact in two modules: let them drift and every
    offline read warns about missing columns and `kill` — the verb an operator
    reaches for when the daemon is down — refuses with exit 1.
    """
    from subfleet.store import SCHEMA_VERSION, Store

    assert offline.KNOWN_SCHEMA_VERSION == SCHEMA_VERSION
    Store(root / "state.sqlite3").close()
    reader = offline.Offline(root)
    assert reader.status()["schema_version"] == SCHEMA_VERSION
    assert reader.newer_schema is None
    assert cli.main(["status"]) == 0
    assert "schema version" not in capsys.readouterr().err


def test_a_store_with_no_tables_is_reported_not_raised(root, capsys):
    """C-17.3 an unreadable store is an exit code, never a traceback."""
    (root / "state.sqlite3").write_bytes(b"not a database at all")
    assert cli.main(["runs"]) == 69
    assert "cannot read" in capsys.readouterr().err


def test_show_out_with_no_deliverable_is_an_operational_error(root, capsys):
    """C-17.4 `--out` that prints nothing must not claim success."""
    build_store(root)
    assert cli.main(["runs", "show", JOB, "--out"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no deliverable recorded" in captured.err


def test_offline_kill_will_not_resolve_a_quarantine(store, capsys):
    """C-5.7, C-3.4 --confirm-dead and --force-release need the daemon's writes."""
    for flag in ("--confirm-dead", "--force-release"):
        assert cli.main(["kill", JOB, flag]) == 69
        captured = capsys.readouterr()
        assert "only the daemon" in captured.err
        assert "subfleet daemon start" in captured.err


def test_offline_kill_refuses_a_pgid_the_guardian_does_not_lead(root, capsys):
    """C-5.4, C-5.1 only a group whose leader is the verified guardian is signalled."""
    build_store(root, guardian_pid=os.getpid(), pgid=4242, proc_start="recorded")
    assert cli.main(["kill", JOB]) == 1
    captured = capsys.readouterr()
    assert "refused" in captured.out
    assert "is not led by the recorded guardian" in captured.err


def test_offline_status_marks_an_old_provider_reading_stale(root, capsys):
    """C-9.1 a provider reading beyond reading_ttl_s is rendered as stale."""
    path = build_store(root)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE readings SET observed_at = '2020-01-01T00:00:00Z'")
    conn.commit()
    conn.close()
    assert cli.main(["status"]) == 0
    assert "42% stale" in capsys.readouterr().out


def test_offline_status_keeps_a_fresh_provider_reading_live(root, capsys):
    """C-9.1 a reading inside reading_ttl_s keeps its provider label."""
    path = build_store(root)
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE readings SET observed_at = ?", (fresh,))
    conn.commit()
    conn.close()
    assert cli.main(["status"]) == 0
    table = capsys.readouterr().out
    assert "42%" in table and "stale" not in table


def test_show_out_prefers_the_accepted_attempts_deliverable(root, capsys):
    """C-8.2 the deliverable is the accepted attempt's, not the earliest one."""
    path = build_store(root)
    first, second = root / "a1.md", root / "a2.md"
    first.write_text("# the failed attempt\n")
    second.write_text("# the accepted attempt\n")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE artifacts SET path = ? WHERE role = 'deliverable'",
                 (str(first),))
    conn.execute(
        "INSERT INTO attempts (attempt_id, job_id, seq, lane_id, model_requested,"
        " state, reserved_at) VALUES (?,?,?,?,?,?,?)",
        (f"{DONE}/a2", DONE, 2, "codex-1", "gpt-6-astra", "succeeded", NOW))
    conn.execute(
        "INSERT INTO artifacts (attempt_id, role, path, sha256, bytes, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (f"{DONE}/a2", "deliverable", str(second), "sha", 20, NOW))
    conn.execute("UPDATE jobs SET accepted_attempt_id = ? WHERE job_id = ?",
                 (f"{DONE}/a2", DONE))
    conn.commit()
    conn.close()
    assert cli.main(["runs", "show", DONE, "--out"]) == 0
    assert capsys.readouterr().out == "# the accepted attempt\n"


def test_a_relative_state_root_still_works(root, monkeypatch, capsys, tmp_path):
    """C-2.1, C-17.3 a relative $SUBFLEET_HOME is resolved, not a ValueError."""
    build_store(root)
    monkeypatch.chdir(root.parent)
    monkeypatch.setenv("SUBFLEET_HOME", root.name)
    assert cli.main(["runs"]) == 0
    assert JOB in capsys.readouterr().out


def test_reads_say_so_when_the_store_is_newer(root, capsys):
    """C-3.5 a store ahead of this CLI is named on every read, not only on kill."""
    path = build_store(root)
    conn = sqlite3.connect(path)
    from subfleet.store import SCHEMA_VERSION
    newer = SCHEMA_VERSION + 1
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                 (newer, NOW))
    conn.commit()
    conn.close()
    for argv in (["runs"], ["status"], ["runs", "show", JOB]):
        assert cli.main(argv) == 0, argv
        assert f"schema version {newer}" in capsys.readouterr().err, argv


def test_reap_prefers_the_core_lanes_identity_check_when_it_lands(store, capsys,
                                                                  monkeypatch):
    """C-5.3 `runs reap` uses subfleet.procs.same_process once that module exists."""
    import sys
    import types
    module = types.ModuleType("subfleet.procs")
    module.same_process = lambda pid, boot, start: False
    monkeypatch.setitem(sys.modules, "subfleet.procs", module)
    assert cli.main(["runs", "reap"]) == 0
    captured = capsys.readouterr()
    assert "identity checked with subfleet.procs" in captured.err
    assert JOB in captured.out


def _write_receipt(root: Path, job_id: str, seq: int, name: str, data: dict) -> None:
    directory = root / "jobs" / job_id / f"a{seq}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(data))


def test_offline_reads_the_guardian_receipts(store, capsys):
    """C-17.5, C-5.2 offline mode reads the receipts beside the store."""
    _write_receipt(store, JOB, 1, "start",
                   {"guardian_pid": 999999, "pgid": 999999, "boot_id": "b",
                    "proc_start": "recorded", "started_at": NOW})
    _write_receipt(store, JOB, 1, "exit",
                   {"rc": 0, "signal": None, "finished_at": NOW, "wall_s": 12.5,
                    "child_pid": 1235})
    assert cli.main(["runs"]) == 0
    # The row still says running; the receipt says the guardian is done.
    assert "EXITED" in capsys.readouterr().out
    assert cli.main(["runs", "show", JOB]) == 0
    text = capsys.readouterr().out
    # C-17.1: the bare form is v1-shaped: the metadata object, then --- out.md ---.
    meta = json.loads(text.split("\n--- out.md ---")[0])
    receipts = meta["attempts"][-1]["receipts"]
    assert receipts["exit"]["rc"] == 0 and receipts["exit"]["wall_s"] == 12.5


def test_a_started_but_unfinished_attempt_shows_its_start_receipt(store, capsys):
    """C-5.2 a start receipt with no exit receipt still reports what it knows."""
    _write_receipt(store, JOB, 1, "start", {"guardian_pid": 999999, "pgid": 999999})
    assert cli.main(["runs"]) == 0
    assert "RUNNING" in capsys.readouterr().out
    assert cli.main(["runs", "show", JOB]) == 0
    meta = json.loads(capsys.readouterr().out.split("\n--- out.md ---")[0])
    assert meta["attempts"][-1]["receipts"]["start"]["pgid"] == 999999


def test_offline_kill_will_not_signal_an_attempt_that_already_exited(store, capsys):
    """C-17.5 a finish receipt means there is nothing left to signal."""
    _write_receipt(store, JOB, 1, "exit", {"rc": 4, "finished_at": NOW})
    assert cli.main(["kill", JOB]) == 0
    captured = capsys.readouterr()
    assert "already-exited" in captured.out
    assert "rc 4" in captured.err and "will finalize it" in captured.err


def test_a_deliverable_on_disk_is_found_without_its_artifact_row(store, capsys):
    """C-17.5 a daemon that died before recording the artifact still leaves the file."""
    directory = store / "jobs" / JOB / "a1"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "deliverable.md").write_text("# written, never recorded\n")
    assert cli.main(["runs", "show", JOB, "--out"]) == 0
    assert capsys.readouterr().out == "# written, never recorded\n"


def test_a_corrupt_receipt_is_ignored_not_fatal(store, capsys):
    """C-17.3 a half-written receipt is skipped, never a traceback."""
    directory = store / "jobs" / JOB / "a1"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text('{"rc": 0, "fini')
    assert cli.main(["runs"]) == 0
    assert "RUNNING" in capsys.readouterr().out
    assert cli.main(["runs", "show", JOB]) == 0
