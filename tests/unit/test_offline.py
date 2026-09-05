"""Offline mode against a temp store: runs, runs show, status, and kill (C-17.5).

Every test names the clause it proves (C-20.5).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from subfleet import cli
from subfleet.offline import Offline, OfflineUnavailable

SCHEMA = Path(__file__).resolve().parents[2] / "subfleet" / "store_schema.sql"
JOB = "20260905-120000-demo"
DONE = "20260905-110000-done"
NOW = "2026-09-05T12:00:00Z"


def build_store(root: Path, *, pgid: int | None = 4242,
                guardian_pid: int | None = 999999,
                proc_start: str | None = "Mon Jan  1 00:00:00 2001") -> Path:
    """A store with one running job, one finished job, a lane, and a reading."""
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
             pgid if rc is None else 4243, "1788531275", proc_start, rc,
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
    build_store(root, guardian_pid=os.getpid(), proc_start=None, pgid=os.getpgid(0))
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
    build_store(root, guardian_pid=os.getpid(), pgid=os.getpgid(0),
                proc_start="recorded")
    monkeypatch.setattr("subfleet.offline.same_process", lambda *a, **k: True)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))
    assert cli.main(["kill", JOB]) == 0
    assert signalled == [(os.getpgid(0), 15)]
    captured = capsys.readouterr()
    assert "signalled" in captured.out
    assert "until a daemon reconciles it" in captured.err


def test_offline_kill_json_emits_one_object(store, capsys):
    """C-17.4 `--json` emits JSON only, offline as well."""
    assert cli.main(["kill", JOB, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["job_id"] == JOB and payload["action"] == "already-dead"


# --- reap ---------------------------------------------------------------------

def test_reap_names_the_orphans_without_writing(store, capsys):
    """C-4.2, C-3.4 `runs reap` reports jobs whose runner is gone; the daemon writes."""
    assert cli.main(["runs", "reap"]) == 0
    captured = capsys.readouterr()
    assert JOB in captured.out and "runner is gone" in captured.out
    assert "identity checked with subfleet.client" in captured.err
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
    assert "schema version 99" in captured.err and "version 1" in captured.err


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
