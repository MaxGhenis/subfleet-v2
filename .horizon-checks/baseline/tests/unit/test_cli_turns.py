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


# --- online: the request `runs` sends (C-16.2, C-17.1, C-25.1) -----------------

#: What the daemon's `capabilities` op answers (service.CAPABILITIES), trimmed.
KIND_AWARE = {"capabilities": lambda request: {"protocol": 1, "capabilities": ["conversations.v1", "jobs.kind.v1"]}}

#: Rows as a daemon that ignores `kind` and `include_turns` answers them: every
#: kind, newest first, each carrying its `kind` column (the daemon selects `*`).
EVERY_KIND = [{"job_id": "t-live", "kind": "turn", "state": "running"},
              {"job_id": "j-resume", "kind": "resume", "state": "succeeded", "rc": 0},
              {"job_id": "t-done", "kind": "turn", "state": "succeeded", "rc": 0},
              {"job_id": "j-dispatch", "kind": "dispatch", "state": "running"}]


def json_ids(capsys) -> list[str]:
    return [json.loads(line)["job_id"] for line in capsys.readouterr().out.splitlines() if line.strip()]


@pytest.mark.parametrize("argv, sent", [
    (["runs"], {"kind": None, "include_turns": False}),
    (["runs", "--kind", "turn"], {"kind": "turn", "include_turns": False}),
    (["jobs", "--kind", "resume"], {"kind": "resume", "include_turns": False}),
    (["runs", "--include-turns"], {"kind": None, "include_turns": True}),
])
def test_c17_1_runs_asks_for_turns_only_explicitly(daemon, capsys, argv, sent):
    """C-17.1, C-26.12 (IR-19): `runs` leaves turns out; `--kind turn` or `--include-turns` asks for them.
    C-25.1: the fields go to a daemon only after its `capabilities` advertised `jobs.kind.v1`."""
    server = daemon({**KIND_AWARE, "list": lambda request: {"jobs": []}})
    assert cli.main(argv) == 0
    assert server.ops() == ["capabilities", "list"]
    args = server.args("list")
    assert {key: args[key] for key in ("kind", "include_turns")} == sent


# A daemon older than the filter: one that predates `capabilities` altogether
# (the fake answers "unknown op", as `protocol.decode_request` does), and one
# that serves conversations but not `jobs.kind.v1` (the b739a12 build).
OLDER_DAEMONS = {"no-capabilities-op": {},
                 "no-kind-capability": {"capabilities": lambda request: {
                     "protocol": 1, "capabilities": ["conversations.v1", "events.v1"]}}}


@pytest.fixture(params=sorted(OLDER_DAEMONS))
def older_daemon(request, daemon):
    """C-16.2 a daemon that drops `kind`/`include_turns` and lists every kind."""
    return daemon({**OLDER_DAEMONS[request.param], "list": lambda request: {"jobs": [dict(row) for row in EVERY_KIND]}})


def test_c25_1_runs_sends_no_kind_fields_to_a_daemon_that_did_not_advertise_them(older_daemon, capsys):
    """C-25.1 `kind` and `include_turns` are sent only after `jobs.kind.v1` was advertised."""
    for argv in (["runs"], ["runs", "--include-turns"], ["runs", "--running", "--json"]):
        assert cli.main(argv) == 0
        args = older_daemon.args("list")
        assert "kind" not in args and "include_turns" not in args
        capsys.readouterr()


def test_c26_12_default_runs_leaves_turns_out_even_when_the_daemon_ignored_the_filter(older_daemon, capsys):
    """C-26.12 (IR-19) turns stay out of `runs` whatever the daemon's version: the rows carry `kind`,
    and the CLI drops the turn rows an older daemon returned, in the table and in `--json`."""
    assert cli.main(["runs", "--json"]) == 0
    assert json_ids(capsys) == ["j-resume", "j-dispatch"]
    assert cli.main(["runs"]) == 0
    captured = capsys.readouterr()
    assert "j-resume" in captured.out and "j-dispatch" in captured.out and "t-live" not in captured.out
    # --last was applied before the turn rows were dropped, so the CLI says the list may be short.
    assert "2 conversation turn job(s) were left out" in captured.err and "--last 20" in captured.err
    assert "daemon stop && subfleet daemon start" in captured.err


def test_c26_12_include_turns_needs_no_field_from_an_older_daemon(older_daemon, capsys):
    """C-26.12 `--include-turns` asks for every kind, which is what a daemon without the filter lists."""
    assert cli.main(["runs", "--include-turns", "--json"]) == 0
    assert json_ids(capsys) == [row["job_id"] for row in EVERY_KIND]


@pytest.mark.parametrize("kind", ["turn", "dispatch"])
def test_c25_1_runs_kind_refuses_a_daemon_that_would_ignore_it(older_daemon, capsys, kind):
    """C-25.1, C-26.12, C-17.3 `--kind` against a daemon without `jobs.kind.v1` would list every job as
    that kind; it exits 69 naming the capability and the restart, and never asks for the list."""
    assert cli.main(["runs", "--kind", kind, "--json"]) == 69
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "jobs.kind.v1" in captured.err and "fix: subfleet daemon stop && subfleet daemon start" in captured.err
    assert "list" not in older_daemon.ops()


def test_c26_12_rows_that_contradict_the_request_are_dropped_on_any_daemon(daemon, capsys):
    """C-26.12 the CLI keeps only rows whose `kind` answers the request, so a daemon that advertised the
    filter and still returned other kinds cannot put them in front of a script; kind-less rows are kept."""
    daemon({**KIND_AWARE, "list": lambda request: {"jobs": [*EVERY_KIND, {"job_id": "j-bare", "state": "running"}]}})
    assert cli.main(["runs", "--kind", "turn", "--json"]) == 0
    assert json_ids(capsys) == ["t-live", "t-done", "j-bare"]
    assert cli.main(["runs", "--json"]) == 0
    assert json_ids(capsys) == ["j-resume", "j-dispatch", "j-bare"]
    assert cli.main(["runs"]) == 0
    assert "left out" not in capsys.readouterr().err           # the daemon honours the filter: no note


def test_c25_1_a_capabilities_failure_other_than_an_unknown_op_is_reported(daemon, capsys):
    """C-25.1, C-17.3 only "unknown op" means an older daemon; any other failure is the daemon's exit code."""
    from subfleet import protocol
    server = daemon({"capabilities": lambda request: protocol.fail(request.id, 1, "store is locked"),
                     "list": lambda request: {"jobs": []}})
    assert cli.main(["runs"]) == 1
    assert "store is locked" in capsys.readouterr().err
    assert "list" not in server.ops()


def test_c17_1_runs_kind_help_names_every_kind_the_code_submits(capsys):
    """C-17.1, C-26.12 `--kind` names the ledger's kinds, and that list is every kind a `SubmitArgs` in the
    package carries, so a new kind (as `handoff` was) cannot go missing from the help."""
    import re
    from pathlib import Path

    from subfleet.contracts import JOB_KINDS
    submitted = set()
    for path in Path(cli.__file__).parent.rglob("*.py"):
        text = path.read_text()
        for call in re.finditer(r"SubmitArgs\(", text):
            found = re.search(r'\bkind="([a-z-]+)"', text[call.end():call.end() + 400])
            if found:
                submitted.add(found.group(1))
    assert submitted == set(JOB_KINDS)
    assert cli.main(["runs", "--help"]) == 0
    usage = re.sub(r"-\s+", "-", " ".join(capsys.readouterr().out.split()))   # argparse wraps at a hyphen
    assert f"only jobs of this kind ({', '.join(JOB_KINDS)})" in usage and "--include-turns" in usage
    assert "--include-turns" in " ".join(cli.__doc__.split())


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


def test_c16_3_offline_request_id_lookup_finds_a_turn(store):
    """C-16.3, C-17.5: offline, as online, a request id names its job whatever its kind."""
    offline = Offline(store)
    assert [row["job_id"] for row in offline.list_jobs(request_id=f"turn:{TURN}:0")] == [TURN]
    assert offline.list_jobs(request_id=f"turn:{TURN}:0", kind="dispatch") == []
    assert TURN not in [row["job_id"] for row in offline.list_jobs()]


def test_c17_5_offline_status_counts_turns_on_their_own_line(store, capsys):
    """C-17.5, C-26.12 (IR-19): offline `status` still says `running jobs: 1` and counts the live turn apart."""
    assert cli.main(["status"]) == 0
    text = capsys.readouterr().out
    assert "running jobs: 1" in text and LIVE_TURN not in text
    assert "conversation turns: 1 live" in text


def test_c4_2_reap_still_sees_a_turn_whose_runner_is_gone(store, capsys, monkeypatch):
    """C-4.2, C-26.12 a turn's runner can be orphaned like any job's, so `runs reap` reconciles turns too:
    the live turn (no attempt, so no runner recorded yet) is reported beside the dispatch job."""
    from subfleet import procs
    monkeypatch.setattr(procs, "liveness", lambda *args: "dead")
    assert cli.main(["runs", "reap", "--json"]) == 0
    reaped = {row["job_id"]: row for row in map(json.loads, capsys.readouterr().out.splitlines())}
    assert JOB in reaped and LIVE_TURN in reaped and TURN not in reaped
    assert reaped[LIVE_TURN]["verdict"] == "no runner recorded yet"
    assert {row["job_id"] for row in Offline(store).list_jobs(running=True)} == {JOB}   # the default leaves it out
