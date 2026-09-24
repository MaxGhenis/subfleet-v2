"""C-17.1, C-26.12 (review IR-19): the CLI's ledger leaves conversations' turns out unless asked.

Online against a fake daemon (the request the CLI sends) and offline against a
temporary store (what the offline reader selects). Every test names its clause.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from subfleet import cli
from subfleet.offline import Offline
from tests.unit.test_offline import DONE, JOB, NOW, build_store

TURN = "20260905-120500-turn-cv-1"
LIVE_TURN = "20260905-121000-turn-cv-2"


def add_turns(root) -> None:
    """Two turn rows as the dispatcher writes them (design §3), one finished and one running."""
    with sqlite3.connect(root / "state.sqlite3") as conn:
        for job_id, state, finished in ((TURN, "succeeded", "2026-09-05T12:06:00Z"), (LIVE_TURN, "running", None)):
            conn.execute(
                "INSERT INTO jobs (job_id, request_id, payload_digest, kind, state, workdir, prompt_path, sandbox,"
                " name, in_place, max_attempts, created_at, started_at, finished_at, rc)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, f"turn:{job_id}:0", "sha", "turn", state, "/Users/x/repo", "/p", "workspace-write",
                 f"turn-cv-{job_id[-1]}", 1, 1, NOW, NOW, finished, 0 if finished else None))


@pytest.fixture
def store(root):
    build_store(root)
    add_turns(root)
    return root


# --- online: the request `runs` sends (C-16.2, C-17.1) -------------------------

@pytest.mark.parametrize("argv, sent", [
    (["runs"], {"kind": None, "include_turns": False}),
    (["runs", "--kind", "turn"], {"kind": "turn", "include_turns": False}),
    (["jobs", "--kind", "resume"], {"kind": "resume", "include_turns": False}),
    (["runs", "--include-turns"], {"kind": None, "include_turns": True}),
])
def test_c17_1_runs_asks_for_turns_only_explicitly(daemon, capsys, argv, sent):
    """C-17.1, C-26.12 (IR-19): `runs` leaves turns out; `--kind turn` or `--include-turns` asks for them."""
    server = daemon({"list": lambda request: {"jobs": []}})
    assert cli.main(argv) == 0
    args = server.args("list")
    assert {key: args[key] for key in ("kind", "include_turns")} == sent


def test_c17_1_kind_and_include_turns_are_exclusive_and_kind_names_something(daemon, capsys):
    """C-17.3 contradictory or empty filters are invalid input, and nothing is asked of the daemon."""
    server = daemon({"list": lambda request: {"jobs": []}})
    assert cli.main(["runs", "--kind", "dispatch", "--include-turns"]) == 2
    assert "not allowed with argument --kind" in capsys.readouterr().err
    assert cli.main(["runs", "--kind", " "]) == 2
    assert "list" not in server.ops()


def test_c26_12_status_counts_turns_apart_from_running_jobs():
    """C-26.12, C-6.11 (IR-19): `daemon.status` carries every job; a live turn is not a running job."""
    rows = [{"job_id": "live-dispatch", "kind": "dispatch", "state": "running"},
            {"job_id": "live-turn", "kind": "turn", "state": "running"},
            {"job_id": "queued-turn", "kind": "turn", "state": "queued"},
            {"job_id": "done-turn", "kind": "turn", "state": "succeeded"}]
    text = cli.format_status({"lanes": [], "jobs": rows})
    assert "running jobs: 1" in text and "live-dispatch" in text
    assert "live-turn" not in text and "queued-turn" not in text
    assert "conversation turns: 2 live" in text
    quiet = cli.format_status({"lanes": [], "jobs": rows[:1]})
    assert "conversation turns" not in quiet


# --- offline: the same ledger from the store (C-17.5) --------------------------

def test_c17_5_offline_runs_leaves_turns_out_unless_asked(store, capsys):
    """C-17.5, C-26.12 (IR-19): offline `runs` selects as the daemon's `list` does."""
    assert cli.main(["runs"]) == 0
    out = capsys.readouterr().out
    assert JOB in out and DONE in out and TURN not in out and LIVE_TURN not in out
    assert cli.main(["runs", "--kind", "turn", "--json"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [row["job_id"] for row in rows] == [LIVE_TURN, TURN] and {row["kind"] for row in rows} == {"turn"}
    assert cli.main(["runs", "--include-turns", "--running"]) == 0
    out = capsys.readouterr().out
    assert JOB in out and LIVE_TURN in out and TURN not in out


def test_c17_5_offline_status_counts_turns_on_their_own_line(store, capsys):
    """C-17.5, C-26.12 (IR-19): offline `status` still says `running jobs: 1` and counts the live turn apart."""
    assert cli.main(["status"]) == 0
    text = capsys.readouterr().out
    assert "running jobs: 1" in text and LIVE_TURN not in text
    assert "conversation turns: 1 live" in text


def test_c4_2_reap_still_sees_a_turn_whose_runner_is_gone(store, capsys, monkeypatch):
    """C-4.2 a turn's runner can be orphaned like any job's, so `runs reap` does not filter turns."""
    from subfleet import procs
    monkeypatch.setattr(procs, "liveness", lambda *args: "dead")
    rows = Offline(store).list_jobs(running=True, last=500, include_turns=True)
    assert {row["job_id"] for row in rows} == {JOB, LIVE_TURN}
    assert cli.main(["runs", "reap", "--json"]) == 0
    reaped = [json.loads(line)["job_id"] for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert JOB in reaped
