"""The CLI against a fake daemon: verbs, aliases, exit codes, JSON, the hint.

Every test names the clause it proves (C-20.5).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from subfleet import cli, protocol
from subfleet.contracts import Exit

JOB = "20260905-120000-demo"


def submit_ok(request: protocol.Request) -> dict:
    return {"job_id": JOB, "request_id": request.args.get("request_id"),
            "created": True, "state": "queued"}


def terminal(state: str = "succeeded", **extra) -> dict:
    return {"jobs": {JOB: {"job_id": JOB, "state": state, **extra}}}


def run_cli(argv: list[str]) -> int:
    return cli.main(argv)


# --- verb table and aliases (C-17.1) -----------------------------------------

def test_bare_subfleet_and_aliases_reach_the_v1_spellings():
    """C-17.1 jobs/show/capacity/notify/resume-codex are aliases, not replacements."""
    assert cli.rewrite_aliases([]) == ["status"]
    assert cli.rewrite_aliases(["--json"]) == ["status", "--json"]
    assert cli.rewrite_aliases(["jobs", "--mine"]) == ["runs", "--mine"]
    assert cli.rewrite_aliases(["show", "x", "--out"]) == ["runs", "show", "x", "--out"]
    assert cli.rewrite_aliases(["capacity"]) == ["status"]
    assert cli.rewrite_aliases(["notify", "hi"]) == ["ping", "hi"]
    assert cli.rewrite_aliases(["resume-codex", "x"]) == ["resume", "x"]
    assert cli.rewrite_aliases(["--help"]) == ["--help"]
    assert cli.rewrite_aliases(["run", "-p", "x"]) == ["run", "-p", "x"]


@pytest.mark.parametrize("argv,handler", [
    ([], "cmd_status"),
    (["status"], "cmd_status"),
    (["capacity"], "cmd_status"),
    (["run", "--task", "build", "--tier", "standard", "hi"], "cmd_run"),
    (["runs"], "cmd_runs"),
    (["jobs", "--mine", "--running", "--last", "5"], "cmd_runs"),
    (["runs", "show", "j"], "cmd_runs"),
    (["show", "j", "--out"], "cmd_runs"),
    (["runs", "reap"], "cmd_runs"),
    (["wait", "j", "--timeout", "1"], "cmd_wait"),
    (["kill", "j", "--wait"], "cmd_kill"),
    (["kill", "j", "--confirm-dead"], "cmd_kill"),
    (["kill", "j", "--force-release"], "cmd_kill"),
    (["resume", "j", "keep going"], "cmd_resume"),
    (["resume-codex", "j"], "cmd_resume"),
    (["lanes"], "cmd_lanes"),
    (["lanes", "list"], "cmd_lanes"),
    (["lanes", "probe"], "cmd_lanes"),
    (["lanes", "enroll", "max@example.org"], "cmd_lanes"),
    (["lanes", "hold", "codex-1", "--until", "2026-09-05T18:00:00Z"], "cmd_lanes"),
    (["lanes", "release", "codex-1"], "cmd_lanes"),
    (["lanes", "transfer", "codex-1", "--to", "v2"], "cmd_lanes"),
    (["why", "j"], "cmd_why"),
    (["why", "--task", "build", "--tier", "hard"], "cmd_why"),
    (["daemon", "start"], "cmd_daemon"),
    (["daemon", "stop"], "cmd_daemon"),
    (["daemon", "status"], "cmd_daemon"),
    (["daemon", "logs", "-n", "5"], "cmd_daemon"),
    (["daemon", "install", "--dry-run"], "cmd_daemon"),
    (["doctor"], "cmd_doctor"),
    (["doctor", "--live"], "cmd_doctor"),
    (["ping", "hello"], "cmd_ping"),
    (["notify", "--session", "s", "hello"], "cmd_ping"),
])
def test_every_verb_and_alias_parses_to_its_handler(argv, handler):
    """C-17.1 every verb and alias parses and reaches the right handler."""
    parsed = cli.build_parser().parse_args(cli.rewrite_aliases(argv))
    assert parsed.handler.__name__ == handler


def test_lanes_actions_reach_the_lanes_op_with_their_arguments(daemon, capsys):
    """C-17.1 lanes list|probe|enroll|hold|release|transfer reach op `lanes`."""
    server = daemon({"lanes": lambda request: {"lanes": []}})
    cases = [
        (["lanes"], {"action": "list"}),
        (["lanes", "list"], {"action": "list"}),
        (["lanes", "probe", "codex-1"], {"action": "probe", "lane_id": "codex-1"}),
        (["lanes", "enroll", "max@example.org"],
         {"action": "enroll", "credential": "max@example.org"}),
        (["lanes", "hold", "codex-1", "--until", "2026-09-05T18:00:00Z"],
         {"action": "hold", "lane_id": "codex-1", "until": "2026-09-05T18:00:00Z"}),
        (["lanes", "release", "codex-1"], {"action": "release", "lane_id": "codex-1"}),
        (["lanes", "transfer", "codex-1", "--to", "v1"],
         {"action": "transfer", "lane_id": "codex-1", "owner": "v1"}),
    ]
    for argv, expected in cases:
        assert run_cli(argv) == 0
        sent = server.args("lanes")
        for key, value in expected.items():
            assert sent[key] == value, argv
    capsys.readouterr()


def test_why_and_ping_reach_their_ops(daemon, capsys):
    """C-17.1 `why` and `ping` carry their arguments to the daemon."""
    server = daemon({
        "why": lambda request: {"decision": {"chosen_model": "astra",
                                             "chosen_lane": "codex-2",
                                             "reason": "opus: no candidate lanes"}},
        "ping": lambda request: {"delivered": True, "name": "cli-1"},
    })
    assert run_cli(["why", "--task", "build", "--tier", "hard", "-x", "a@b.c"]) == 0
    assert server.args("why")["task"] == "build"
    assert server.args("why")["tier"] == "hard"
    assert server.args("why")["exclusions"] == ["a@b.c"]
    assert "astra" in capsys.readouterr().out
    assert run_cli(["notify", "--session", "s-1", "hello"]) == 0
    assert server.args("ping") == {"text": "hello", "session_id": "s-1"}


# --- run (C-17.2, C-17.4, C-17.6) --------------------------------------------

def test_run_inside_a_claude_session_is_detached_and_prints_the_hint(
        daemon, monkeypatch, capsys, root, workdir):
    """C-17.6 inside a Claude session `run` returns the job id at once with the hint."""
    server = daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-9")
    assert run_cli(["run", "--task", "build", "--tier", "standard",
                    "-C", str(workdir), "do the thing"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == JOB                    # C-17.4 stdout is the contract
    hint = captured.err.splitlines()
    assert any(line.startswith("  out: ") for line in hint)
    assert any(line.startswith("  log: ") for line in hint)
    assert any(f"done → subfleet wait {JOB}" in line for line in hint)
    assert any(line.startswith("  status: subfleet runs --mine") for line in hint)
    args = server.args("submit")
    assert args["task"] == "build" and args["tier"] == "standard"
    assert args["caller_session"] == "sess-9"
    assert args["kind"] == "dispatch"
    assert Path(args["prompt_path"]).read_text() == "do the thing\n"


def test_run_prints_the_request_id_back_and_generates_a_uuid4(daemon, capsys, root, workdir):
    """C-1.5 the request id is a UUID4 unless given, and is printed back."""
    server = daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "-d", "--json",
                    "hi"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    import uuid
    assert uuid.UUID(payload["request_id"]).version == 4
    assert server.args("submit")["request_id"] == payload["request_id"]
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "-d", "--json",
                    "--request-id", "fixed-1", "hi"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["request_id"] == "fixed-1"


def test_run_json_is_json_objects_and_no_prose(daemon, capsys, root, workdir):
    """C-17.4 --json emits one JSON object per line and no prose.

    Detached, that is the single dispatch object. Waiting inline, the dispatch
    object still lands on stdout at once — the job id is the contract — and the
    terminal state follows it as a second object.
    """
    daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "-d", "--json",
                    "hi"]) == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["job_id"] == JOB and payload["run_id"] == JOB
    assert captured.err == ""
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "--json", "hi"]) == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert [json.loads(line)["job_id"] for line in lines] == [JOB, JOB]
    assert json.loads(lines[-1])["state"] == "succeeded"
    assert captured.err == ""


def test_run_wait_returns_the_jobs_mapped_rc(daemon, capsys, root, workdir):
    """C-17.3 `run --wait` returns the job's rc through the one exit-code table."""
    daemon({"submit": submit_ok,
            "wait": lambda request: terminal("failed", rc=4)})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "--wait", "hi"]) == 4
    capsys.readouterr()


def test_run_outside_a_claude_session_waits_inline(daemon, capsys, root, workdir):
    """C-17.6 outside a session `run` keeps v1's synchronous behaviour."""
    daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 0
    assert "waiting inline" in capsys.readouterr().err


def test_run_no_wait_queue_exits_75(daemon, capsys, root, monkeypatch, workdir):
    """C-17.3 exit 75 is queued, only with --json and --no-wait-queue."""
    daemon({"submit": lambda request: {"job_id": JOB, "created": True,
                                       "state": "queued"}})
    monkeypatch.setenv("CLAUDECODE", "1")
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "--json",
                    "--no-wait-queue", "hi"]) == 75
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "--json", "hi"]) == 0
    capsys.readouterr()


def test_run_dry_run_prints_the_decision_and_dispatches_nothing(daemon, capsys, root, workdir):
    """C-11.5 --dry-run and --why print the decision without dispatching."""
    server = daemon({"submit": lambda request: {
        "decision": {"chain": ["opus", "astra"], "chosen_model": "astra",
                     "chosen_lane": "codex-2",
                     "reason": "opus: no candidate lanes after exclusions; promoted"}}})
    assert run_cli(["run", "--task", "build", "--tier", "standard",
                    "-C", str(workdir), "--dry-run", "hi"]) == 0
    assert server.args("submit")["dry_run"] is True
    assert "promoted" in capsys.readouterr().out
    assert run_cli(["run", "--task", "build", "--tier", "standard",
                    "-C", str(workdir), "--why", "hi"]) == 0
    assert server.args("submit")["dry_run"] is True
    assert "chosen_model" in capsys.readouterr().out


def test_run_prompt_file_is_sent_by_path(daemon, root, capsys, workdir):
    """C-6.1 `-p PROMPTFILE` sends the resolved path, not the bytes."""
    server = daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    prompt = workdir / "prompt.md"
    prompt.write_text("from a file\n")
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "-p", str(prompt)]) == 0
    assert server.args("submit")["prompt_path"] == str(prompt.resolve())
    capsys.readouterr()


def test_run_carries_every_flag_in_the_table(daemon, root, capsys, workdir):
    """C-17.2 the `run` flags reach `submit` with the argument names of C-16.2."""
    server = daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    out_dir = workdir / "out"
    out_dir.mkdir()
    assert run_cli([
        "run", "--task", "review", "--tier", "hard", "-C", str(workdir),
        "-o", str(out_dir / "r.md"), "-n", "review-1", "-s", "workspace-write",
        "-x", "a@b.c", "-x", "d@e.f", "--allow-desktop", "--in-place",
        "--independent", "--parent", "20260905-000000-parent",
        "--no-preamble", "--request-id", "rq-1", "hi"]) == 0
    args = server.args("submit")
    assert args["task"] == "review" and args["tier"] == "hard"
    assert args["out_path"] == str(out_dir / "r.md")
    assert args["name"] == "review-1" and args["sandbox"] == "workspace-write"
    assert args["exclusions"] == ["a@b.c", "d@e.f"]
    assert args["allow_desktop"] and args["in_place"] and args["independent"]
    assert args["parent_job_id"] == "20260905-000000-parent"
    assert args["no_preamble"] and args["request_id"] == "rq-1"
    capsys.readouterr()


def test_pinned_lane_comes_from_a_or_h(daemon, root, capsys, workdir):
    """C-17.2 `-a EMAIL` and `-H CODEX_HOME` pin the lane."""
    server = daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "-a", "max@example.org", "-C", str(workdir), "hi"]) == 0
    assert server.args("submit")["pinned_lane"] == "max@example.org"
    assert run_cli(["run", "-H", "/Users/x/.codex-3", "-C", str(workdir), "hi"]) == 0
    assert server.args("submit")["pinned_lane"] == "/Users/x/.codex-3"
    capsys.readouterr()


# --- deprecations (C-17.2) ----------------------------------------------------

def test_retired_sol_is_remapped_to_astra_with_a_note(daemon, root, capsys, workdir):
    """C-17.2 `-m sol` is accepted, remapped to astra, and noted on stderr."""
    server = daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "-m", "sol", "-C", str(workdir), "hi"]) == 0
    assert server.args("submit")["pinned_model"] == "astra"
    assert "retired" in capsys.readouterr().err


def test_legacy_task_classes_are_accepted_with_a_note(daemon, root, capsys, workdir):
    """C-17.2 `-t CLASS` is deprecated but accepted; review routes at the standard tier."""
    server = daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "-t", "review", "-C", str(workdir), "hi"]) == 0
    args = server.args("submit")
    assert args["task"] == "review" and args["tier"] == "standard"
    assert "-t review is deprecated" in capsys.readouterr().err
    assert run_cli(["run", "-t", "fable", "-C", str(workdir), "hi"]) == 0
    assert server.args("submit")["pinned_model"] == "fable"
    capsys.readouterr()


def test_overflow_is_accepted_and_ignored(daemon, root, capsys, workdir):
    """C-17.2 `--overflow` is deprecated, accepted, and noted."""
    daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "--overflow", "hi"]) == 0
    assert "--overflow is deprecated" in capsys.readouterr().err


# --- exit codes (C-17.3) ------------------------------------------------------

@pytest.mark.parametrize("job,expected", [
    ({"state": "succeeded", "rc": 0}, 0),
    ({"state": "cancelled", "rc": 130}, 130),
    ({"state": "lost", "rc": None}, 125),
    ({"state": "failed", "rc": 3}, 3),
    ({"state": "failed", "rc": 7}, 7),
    ({"state": "failed", "rc": 69}, 69),
    ({"state": "failed", "rc": 42}, 1),
    ({"state": "failed", "rc": None}, 1),
])
def test_exit_code_table_has_one_meaning_each(job, expected, capsys):
    """C-17.3 the job's rc maps onto the one table; anything else is 1."""
    assert cli.exit_for_job({"job_id": JOB, **job}) == expected
    capsys.readouterr()


def test_wait_timeout_exits_124(daemon, capsys):
    """C-15.4, C-17.3 `wait --timeout` on a job that never finishes exits 124."""
    daemon({"wait": lambda request: {"timeout": True}})
    assert run_cli(["wait", JOB, "--timeout", "1"]) == 124
    assert "timeout" in capsys.readouterr().err


def test_wait_lost_is_125_and_cancelled_is_130(daemon, capsys):
    """C-17.3 a lost job exits 125 and a cancelled job exits 130."""
    daemon({"wait": lambda request: terminal("lost")})
    assert run_cli(["wait", JOB]) == 125
    capsys.readouterr()


def test_wait_cancelled_is_130(daemon, capsys):
    """C-17.3 a cancelled job exits 130."""
    daemon({"wait": lambda request: terminal("cancelled")})
    assert run_cli(["wait", JOB]) == 130
    capsys.readouterr()


def test_wait_deadline_never_exceeds_the_long_poll_cap(daemon, capsys):
    """C-15.4 each `wait` call carries a deadline of at most 60 s."""
    server = daemon({"wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["wait", JOB]) == 0
    assert server.args("wait")["deadline_s"] <= 60
    assert server.args("wait")["job_ids"] == [JOB]
    capsys.readouterr()


def test_wait_mine_needs_a_session_id(daemon, capsys):
    """C-17.3 invalid input is exit 2."""
    daemon({"wait": lambda request: terminal()})
    assert run_cli(["wait", "--mine"]) == 2
    assert "CLAUDE_CODE_SESSION_ID" in capsys.readouterr().err


def test_wait_mine_sends_the_session_id(daemon, monkeypatch, capsys):
    """C-17.1 `wait --mine` resolves the job set server-side."""
    server = daemon({"wait": lambda request: terminal("succeeded", rc=0)})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-7")
    assert run_cli(["wait", "--mine"]) == 0
    assert server.args("wait")["mine"] == "sess-7"
    capsys.readouterr()


def test_daemon_error_response_carries_its_exit_code(daemon, capsys, root, workdir):
    """C-16.1, C-17.3 an `ok: false` response exits with the daemon's code."""
    daemon({"submit": lambda request: protocol.fail(
        request.id, Exit.REFUSED, "a workdir under /tmp is refused",
        "pass --allow-tmp")})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 7
    captured = capsys.readouterr()
    assert "refused" in captured.err and "--allow-tmp" in captured.err


def test_malformed_response_is_an_operational_error(daemon, capsys, root, workdir):
    """C-16.1, C-17.3 a malformed response line is exit 1, not a traceback."""
    daemon({"submit": lambda request: b"{not json at all\n"})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 1
    assert "malformed response" in capsys.readouterr().err


def test_empty_response_is_an_operational_error(daemon, capsys, root, workdir):
    """C-16.1 a connection closed without a response line is exit 1."""
    daemon({"submit": lambda request: b""})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 1
    assert "without a response" in capsys.readouterr().err


def test_invalid_input_is_exit_2(daemon, capsys, root, workdir):
    """C-17.3 exit 2 is invalid input, checked before any daemon call."""
    daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "--task", "build", "-C", str(workdir), "hi"]) == 2
    assert "--tier is required" in capsys.readouterr().err
    assert run_cli(["run", "-m", "opus", "-C", str(workdir / "missing"), "hi"]) == 2
    assert "not a directory" in capsys.readouterr().err
    assert run_cli(["run", "-C", str(workdir), "hi"]) == 2
    assert "name the work or pin the lane" in capsys.readouterr().err


def test_a_tmp_workdir_is_refused_with_exit_7(daemon, capsys):
    """C-2.4, C-6.5, C-17.3 a workdir under /tmp is refused and names the fix."""
    daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "-m", "opus", "-C", "/tmp", "hi"]) == 7
    captured = capsys.readouterr()
    assert "/tmp is refused" in captured.err and "--allow-tmp" in captured.err


# --- daemon liveness (C-5.8) --------------------------------------------------

def test_a_lock_whose_holder_is_dead_is_no_daemon(daemon, root, capsys, workdir):
    """C-5.8 the CLI treats a lock with a dead recorded identity as no daemon."""
    daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})                 # a socket that would answer
    (root / "daemon.lock").write_text(json.dumps(
        {"pid": 999999, "boot_id": "1", "proc_start": "Mon Jan  1 00:00:00 2001",
         "version": "2.0.0a0"}))
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 69
    captured = capsys.readouterr()
    assert "no longer running" in captured.err
    assert "subfleet daemon start" in captured.err


def test_daemon_down_run_exits_69_and_names_the_fix(root, capsys, workdir):
    """C-17.5 with no daemon, `run` exits 69 and prints `subfleet daemon start`."""
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 69
    assert "subfleet daemon start" in capsys.readouterr().err


# --- runs, kill, resume -------------------------------------------------------

def test_runs_renders_the_table_and_json_lines(daemon, capsys):
    """C-17.1, C-17.4 `runs` prints the ledger; --json emits one object per line."""
    rows = [
        {"job_id": JOB, "state": "running", "provider": "claude", "model": "opus",
         "lane_id": "claude-2", "workdir": "/Users/x/repo", "caller_session": "sess-1"},
        {"job_id": "20260905-110000-other", "state": "succeeded", "rc": 0,
         "provider": "codex", "model": "astra", "lane_id": "codex-3",
         "out_bytes": 2048, "duration_s": 12.5, "workdir": "/Users/x/other"},
    ]
    server = daemon({"list": lambda request: {"jobs": rows}})
    assert run_cli(["runs", "--last", "5", "--running"]) == 0
    table = capsys.readouterr().out
    assert "RUNNING" in table and JOB in table and "astra" in table
    assert server.args("list") == {"mine": None, "running": True, "last": 5}
    assert run_cli(["jobs", "--json"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2 and json.loads(lines[0])["job_id"] == JOB


def test_runs_mine_needs_a_session_id(daemon, capsys):
    """C-17.3 `runs --mine` outside a Claude session is invalid input."""
    daemon({"list": lambda request: {"jobs": []}})
    assert run_cli(["runs", "--mine"]) == 2
    assert "CLAUDE_CODE_SESSION_ID" in capsys.readouterr().err


def test_runs_show_prints_metadata_and_out(daemon, root, capsys):
    """C-17.1, C-17.4 `runs show --out` puts the deliverable on stdout."""
    deliverable = root / "deliverable.md"
    deliverable.write_text("# the answer\n")
    job = {"job_id": JOB, "state": "succeeded", "rc": 0, "workdir": "/Users/x/repo",
           "model_requested": "opus", "lane_id": "claude-2",
           "artifacts": [{"role": "deliverable", "path": str(deliverable),
                          "bytes": 13}]}
    server = daemon({"show": lambda request: job})
    assert run_cli(["show", JOB]) == 0
    assert JOB in capsys.readouterr().out
    assert server.args("show") == {"job_id": JOB}
    assert run_cli(["runs", "show", JOB, "--out"]) == 0
    assert capsys.readouterr().out == "# the answer\n"
    assert run_cli(["runs", "show", JOB, "--json"]) == 0
    assert json.loads(capsys.readouterr().out.strip())["job_id"] == JOB


def test_kill_reaches_the_kill_op_with_its_resolutions(daemon, capsys):
    """C-7.1, C-5.7 `kill` records the cancel; --confirm-dead/--force-release ride along."""
    server = daemon({"kill": lambda request: {"status": "cancel requested"},
                     "wait": lambda request: terminal("cancelled")})
    assert run_cli(["kill", JOB]) == 0
    assert server.args("kill") == {"job_id": JOB, "confirm_dead": False,
                                   "force_release": False, "operator_note": None}
    assert run_cli(["kill", JOB, "--confirm-dead", "--note", "checked"]) == 0
    assert server.args("kill")["confirm_dead"] is True
    assert server.args("kill")["operator_note"] == "checked"
    assert run_cli(["kill", JOB, "--force-release"]) == 0
    assert server.args("kill")["force_release"] is True
    assert run_cli(["kill", JOB, "--wait"]) == 130
    capsys.readouterr()


def test_resume_submits_a_continuation_pinned_to_the_source_lane(daemon, root, capsys):
    """C-17.1 `resume` continues a job on the lane that owns its provider thread."""
    server = daemon({
        "show": lambda request: {"job_id": JOB, "workdir": str(root),
                                 "sandbox": "workspace-write", "lane_id": "codex-3",
                                 "name": "demo", "out_path": str(root / "o.md")},
        "submit": submit_ok})
    assert run_cli(["resume-codex", JOB, "keep going"]) == 0
    args = server.args("submit")
    assert args["kind"] == "resume" and args["parent_job_id"] == JOB
    assert args["pinned_lane"] == "codex-3" and args["workdir"] == str(root)
    assert Path(args["prompt_path"]).read_text() == "keep going\n"
    assert capsys.readouterr().out.strip() == JOB


def test_status_renders_lanes_readings_and_running_jobs(daemon, capsys):
    """C-17.1 `subfleet` and `subfleet status` print the fleet table."""
    daemon({"daemon.status": lambda request: {
        "lanes": [{"lane_id": "codex-1", "provider": "codex",
                   "account_key": "codex:acct-1", "owner": "v2", "in_flight": 1}],
        "readings": [{"lane_id": "codex-1", "scope": "account", "window": "five_hour",
                      "utilization": 0.42, "label": "provider"}],
        "closures": [{"lane_id": "codex-2", "scope": "account",
                      "until_at": "2026-09-05T18:00:00Z", "reason": "provider-limit",
                      "clock_source": "reported"}],
        "running": []}})
    assert run_cli([]) == 0
    table = capsys.readouterr().out
    assert "codex-1" in table and "42%" in table and "closures" in table
    assert run_cli(["capacity", "--json"]) == 0
    assert "codex-1" in json.loads(capsys.readouterr().out.strip())["lanes"][0]["lane_id"]


def test_status_falls_back_to_the_other_ops_when_daemon_status_is_thin(daemon, capsys):
    """C-16.2 a daemon that answers `daemon.status` without rows is still rendered."""
    server = daemon({
        "daemon.status": lambda request: {"version": "2.0.0a0"},
        "lanes": lambda request: {"lanes": []},
        "readings": lambda request: {"readings": []},
        "list": lambda request: {"jobs": []},
    })
    assert run_cli(["status"]) == 0
    assert set(server.ops()) == {"daemon.status", "lanes", "readings", "list"}
    assert "no lanes enrolled" in capsys.readouterr().out


def test_json_wait_output_stays_prose_free_on_a_provider_rc(daemon, capsys, workdir):
    """C-17.4 a provider rc outside the table maps to 1 without prose under --json."""
    daemon({"submit": submit_ok,
            "wait": lambda request: terminal("failed", rc=42)})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "--json", "hi"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out.splitlines()[-1])["rc"] == 42
    assert run_cli(["wait", JOB]) == 1
    assert "provider rc 42" in capsys.readouterr().err


def test_the_identity_check_runs_once_per_client(root, monkeypatch):
    """C-5.8 the lock identity check costs one ps per process, not one per call."""
    from subfleet import client as client_module
    (root / "daemon.lock").write_text(json.dumps({"pid": os.getpid()}))
    calls: list[int] = []
    monkeypatch.setattr(client_module, "same_process",
                        lambda *a, **k: calls.append(1) or None)
    probe = client_module.Client(root)
    for _ in range(4):
        probe.check_available()
    assert len(calls) == 1


def test_boot_id_is_read_once(monkeypatch):
    """C-5.3 boot time cannot change under a running process, so it is read once."""
    from subfleet import client as client_module
    monkeypatch.setattr(client_module, "_BOOT_ID", [])
    reads: list[int] = []
    monkeypatch.setattr(client_module, "_read_boot_id",
                        lambda: reads.append(1) or "1788531275")
    assert client_module.boot_id() == "1788531275"
    assert client_module.boot_id() == "1788531275"
    assert len(reads) == 1


def test_a_result_whose_shape_drifted_renders_instead_of_raising(daemon, capsys):
    """C-16.2 unknown fields are ignored; a drifting shape must not raise."""
    daemon({"list": lambda request: {"jobs": ["not a row", {"job_id": JOB,
                                                            "state": "running"}]},
            "daemon.status": lambda request: {"lanes": "not a list",
                                              "readings": None, "running": 7},
            "show": lambda request: {"job_id": JOB, "state": "succeeded",
                                     "artifacts": "not a list",
                                     "attempts": {"seq": 1, "state": "succeeded"}}})
    assert run_cli(["runs"]) == 0
    assert JOB in capsys.readouterr().out
    assert run_cli(["status"]) == 0
    assert "no lanes enrolled" in capsys.readouterr().out
    assert run_cli(["runs", "show", JOB]) == 0
    assert JOB in capsys.readouterr().out
