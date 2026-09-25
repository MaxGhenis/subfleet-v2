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
    # A `transfer` answer carries the transfer it performed; the CLI refuses to
    # report one the daemon did not do (subfleet/lanes_transfer.py).
    # Likewise an `enroll` answer carries the lane it made and a `hold`/`release`
    # answer names the lane it acted on; the CLI refuses to report a no-op as done.
    def answer(request):
        action = request.args.get("action")
        result = {"lanes": []}
        if action == "transfer":
            result["transfer"] = {"lane_id": request.args.get("lane_id"), "from": "v2",
                                  "to": request.args.get("owner"), "changed": True,
                                  "applied": True, "dry_run": False, "diff": "", "edits": [],
                                  "follow_up": []}
        elif action == "enroll":
            result["enrolled"] = {"lane_id": "claude-9", "provider": "claude", "owner": "v2",
                                  "account_key": "claude:a:o", "label": request.args.get("credential")}
        elif action in ("hold", "release"):
            result["held" if action == "hold" else "released"] = request.args.get("lane_id")
            result["closures"] = []
        return result
    server = daemon({"lanes": answer})
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


@pytest.mark.parametrize("answer,expected", [
    ({"decision": None, "queue": ["Job: j is failed"], "route_error": None,
      "refused": "RouteError: pinned_lane: 'a@b.c' names 2 lanes (claude-1, codex-1)"},
     ["Job: j is failed", "Refused at admission: RouteError: pinned_lane: 'a@b.c' names 2 lanes (claude-1, codex-1)"]),
    ({"decision": None, "queue": ["Job: j is queued", "Held: no admission pass has reached this job yet"],
      "route_error": "KeyError: 'utilization'", "refused": None},
     ["Job: j is queued", "Held: no admission pass has reached this job yet",
      "Decision: none; this job's route could not be evaluated: KeyError: 'utilization'"]),
    ({"decision": None, "queue": ["Job: j is cancelled"]}, ["Job: j is cancelled", "No decision recorded."]),
])
def test_why_prints_a_refusal_or_a_route_error_not_no_decision(daemon, capsys, answer, expected):
    """C-6.12 on 2026-09-22 `why` said "No decision recorded." for the jobs that stopped admission."""
    daemon({"why": lambda request: answer})
    assert run_cli(["why", "j"]) == 0
    assert capsys.readouterr().out.splitlines() == expected


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


def test_c6_11_why_never_prints_null(daemon, capsys):
    """C-6.11 incident 2026-09-20: a queued job with no decision row printed `null`."""
    answers = iter([
        {"decision": None, "text": "No decision recorded."},                       # a daemon that predates `queue`
        {"decision": None, "queue": [f"Job: {JOB} is cancelled"], "text": "unused"},
        {"decision": {"chain": ["opus"], "chosen_model": None, "chosen_lane": None, "reason": "no lane"},
         "queue": [f"Job: {JOB} is queued", "Held: held behind 20260920-155803-older, an older standard job"]},
    ])
    daemon({"why": lambda request: next(answers)})
    assert run_cli(["why", JOB]) == 0
    assert capsys.readouterr().out.strip() == "No decision recorded."
    assert run_cli(["why", JOB]) == 0
    assert capsys.readouterr().out.strip().splitlines() == [f"Job: {JOB} is cancelled", "No decision recorded."]
    assert run_cli(["why", JOB]) == 0
    shown = capsys.readouterr().out
    assert shown.startswith(f"Job: {JOB} is queued\nHeld: held behind 20260920-155803-older")
    assert "chain: opus" in shown and "null" not in shown


def test_c6_11_status_counts_live_jobs_not_the_whole_store():
    """C-6.11 incident 2026-09-20: `daemon.status` carries every job as `jobs`; status said `running jobs: 519`."""
    rows = ([{"job_id": f"done-{n}", "state": state} for n, state in
             enumerate(["succeeded"] * 5 + ["failed", "cancelled", "lost"])]
            + [{"job_id": "live-running", "state": "running"}, {"job_id": "live-waiting", "state": "waiting"},
               {"job_id": "live-queued", "state": "queued"}])
    text = cli.format_status({"lanes": [], "jobs": rows})
    assert "running jobs: 3" in text
    assert "live-queued" in text and "done-0" not in text
    assert "admission:" not in text                                   # nothing pending and idle: no line
    idle = cli.format_status({"lanes": [], "jobs": rows, "admission": {
        "pending": 2, "idle_for_s": 4210, "reasons": {"reserve:fable:unmeasured": 9, "behind-older-job": 4},
        "open_lanes": ["claude-2", "claude-3"]}})
    assert ("admission: 2 pending, none placed for 4210 s; 2 lanes open; "
            "reserve:fable:unmeasured x9, behind-older-job x4") in idle
    assert "admission:" not in cli.format_status({"lanes": [], "jobs": rows, "admission": {
        "pending": 0, "idle_for_s": None, "reasons": {}, "open_lanes": []}})


def test_why_renders_exclusion_from_real_policy_decision():
    """C-11.5 why prints the actual policy decision's rejected lane and reason."""
    from dataclasses import asdict
    from subfleet.policy import DEFAULT_POLICY_PATH, load_policy, pick

    policy = load_policy(DEFAULT_POLICY_PATH)
    lanes = [{"lane_id": f"claude-{number}", "provider": "claude", "owner": "v2"}
             for number in (1, 2)]
    decision = asdict(pick(policy, lanes, pinned_model="fable",
                           exclusions=("claude-1",), policy_digest="recorded-policy"))
    # Match the JSON socket representation consumed by the CLI.
    decision = json.loads(json.dumps(decision))
    assert decision["chosen_lane"] == "claude-2"
    rendered = cli._format_decision(decision)
    assert "claude-1: excluded" in rendered
    assert "chosen: fable on claude-2" in rendered
    assert "policy: recorded-policy" in rendered


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
    out_line = next(line for line in hint if line.startswith("  out: "))
    log_line = next(line for line in hint if line.startswith("  log: "))
    assert out_line.endswith(f"{root}/jobs/{JOB}/a1/deliverable.md")
    assert log_line.endswith(f"{root}/jobs/{JOB}/a1/lane.log")
    assert any(f"done → subfleet wait {JOB}" in line for line in hint)
    assert any(line == f"  status: subfleet runs --mine · details: subfleet runs "
               f"show {JOB} · cancel: subfleet kill {JOB}" for line in hint)
    args = server.args("submit")
    assert args["task"] == "build" and args["tier"] == "standard"
    assert args["caller_session"] == "sess-9"
    assert args["kind"] == "dispatch"
    assert Path(args["prompt_path"]).read_text() == "do the thing\n"


def test_the_hint_names_the_o_path_when_one_was_given(daemon, monkeypatch, capsys,
                                                     root, workdir):
    """C-17.6 the hint's out path is the caller's -o when there is one."""
    daemon({"submit": submit_ok})
    monkeypatch.setenv("CLAUDECODE", "1")
    target = workdir / "answer.md"
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "-o", str(target),
                    "hi"]) == 0
    assert f"  out: {target}" in capsys.readouterr().err.splitlines()


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
    """C-17.2, C-16.2 `-p PROMPTFILE` puts the resolved path in SubmitArgs."""
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


def test_run_says_where_a_writable_job_writes(daemon, root, capsys, workdir):
    """Review of d261: the caller hears the job writes in its own worktree."""
    def submitted(request):
        return {**submit_ok(request), "sandbox": "workspace-write", "worktree": f"/state/worktrees/{JOB}"}
    daemon({"submit": submitted, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "--task", "build", "--tier", "standard", "-C", str(workdir), "hi"]) == 0
    err = capsys.readouterr().err
    assert f"writes in /state/worktrees/{JOB}" in err and "--in-place writes here" in err


def test_run_without_s_lets_the_policy_choose_the_sandbox(daemon, root, capsys, workdir):
    """d261: no -s sends `policy`, so the daemon applies the task's permissions
    (build writes); -s still names it outright."""
    server = daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "--task", "build", "--tier", "standard", "-C", str(workdir), "hi"]) == 0
    assert server.args("submit")["sandbox"] == "policy"
    assert run_cli(["run", "--task", "build", "--tier", "standard", "-s", "read-only", "-C", str(workdir), "hi"]) == 0
    assert server.args("submit")["sandbox"] == "read-only"
    capsys.readouterr()


def test_pinned_lane_comes_from_a_or_h(daemon, root, capsys, workdir):
    """C-17.2 `-a EMAIL` and `-H CODEX_HOME` pin the lane."""
    server = daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "-a", "max@example.org", "-C", str(workdir), "hi"]) == 0
    assert server.args("submit")["pinned_lane"] == "max@example.org"
    assert server.args("submit")["pinned_provider"] == "claude"      # C-11.2: -a names a Claude account
    assert run_cli(["run", "-H", "/Users/x/.codex-3", "-C", str(workdir), "hi"]) == 0
    assert server.args("submit")["pinned_lane"] == "/Users/x/.codex-3"
    assert server.args("submit")["pinned_provider"] == "codex"
    assert run_cli(["run", "-m", "astra", "-C", str(workdir), "hi"]) == 0
    assert server.args("submit")["pinned_provider"] is None
    capsys.readouterr()


@pytest.mark.parametrize("pins", [["-a", "operator@example.org", "-m", "fable"],
                                  ["-H", "/Users/x/.codex-3", "-m", "astra"]])
@pytest.mark.parametrize("reason", ["Account page checked at 09:00; allow a probe.", "x" * 2000])
def test_unmeasured_reserve_authorization_is_explicit_and_carried_on_wire(
        daemon, workdir, pins, reason):
    server = daemon({"submit": submit_ok})
    assert run_cli(["run", *pins, "-C", str(workdir), "-d",
                    "--allow-unmeasured-reserve", reason, "hi"]) == 0
    assert server.args("submit")["unmeasured_reserve_reason"] == reason


@pytest.mark.parametrize("pins", [[], ["-m", "fable"], ["-a", "operator@example.org"],
                                  ["-H", "/Users/x/.codex-3"],
                                  ["-a", "operator@example.org", "-t", "fable"]])
def test_unmeasured_reserve_requires_explicit_model_and_lane_before_any_request(
        daemon, workdir, root, capsys, pins):
    server = daemon({"submit": submit_ok})
    assert run_cli(["run", *pins, "-C", str(workdir), "-d",
                    "--allow-unmeasured-reserve", "Operator checked usage.", "hi"]) == Exit.INVALID_INPUT
    assert "requires explicit -m and -a/-H" in capsys.readouterr().err
    assert server.requests == []
    assert not (root / "inbox").exists()


@pytest.mark.parametrize("reason", ["", " \t\n", "x" * 2001])
def test_unmeasured_reserve_requires_bounded_nonblank_evidence(
        daemon, workdir, capsys, reason):
    server = daemon({"submit": submit_ok})
    assert run_cli(["run", "-a", "operator@example.org", "-m", "fable", "-C", str(workdir),
                    "-d", "--allow-unmeasured-reserve", reason, "hi"]) == Exit.INVALID_INPUT
    assert "nonblank reason of at most 2000 characters" in capsys.readouterr().err
    assert server.requests == []


def test_unmeasured_reserve_has_no_environment_or_previous_run_default(
        daemon, workdir, monkeypatch):
    server = daemon({"submit": submit_ok})
    monkeypatch.setenv("SUBFLEET_ALLOW_UNMEASURED_RESERVE", "Inherited reason")
    monkeypatch.setenv("SUBFLEET_UNMEASURED_RESERVE_REASON", "Inherited reason")
    base = ["run", "-a", "operator@example.org", "-m", "fable", "-C", str(workdir), "-d"]
    assert run_cli([*base, "--allow-unmeasured-reserve", "This job only.", "hi"]) == 0
    assert server.args("submit")["unmeasured_reserve_reason"] == "This job only."
    assert run_cli([*base, "hi"]) == 0
    assert server.args("submit")["unmeasured_reserve_reason"] is None


def test_unmeasured_reserve_help_explains_probe_scope_and_known_limits(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["run", "--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--allow-unmeasured-reserve REASON" in help_text
    assert "same-model probe" in help_text
    assert "does not override known limits" in help_text


def test_unmeasured_reserve_evidence_remains_visible_in_job_metadata(daemon, capsys):
    reason = "Operator checked account page at 09:00."
    daemon({"show": lambda request: {"job": {"job_id": JOB, "state": "queued",
                                              "unmeasured_reserve_reason": reason}}})
    assert run_cli(["runs", "show", JOB, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["job"]["unmeasured_reserve_reason"] == reason


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


@pytest.mark.parametrize("state,expected", [("lost", 125), ("cancelled", 130),
                                            ("succeeded", 0)])
def test_wait_maps_each_terminal_state(daemon, capsys, state, expected):
    """C-17.3 a lost job exits 125, a cancelled one 130, a succeeded one 0."""
    daemon({"wait": lambda request: terminal(state, rc=0 if state == "succeeded"
                                             else None)})
    assert run_cli(["wait", JOB]) == expected
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
    assert "is stale" in captured.err
    assert "no live process with pid 999999" in captured.err
    assert "subfleet daemon start" in captured.err
    from subfleet.client import boot_id
    (root / "daemon.lock").write_text(json.dumps(
        {"pid": 999999, "boot_id": boot_id(),
         "proc_start": "Mon Jan  1 00:00:00 2001"}))
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 69
    assert "no live process with pid 999999" in capsys.readouterr().err


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
    server = daemon({"list": lambda request: {"jobs": rows},
                     "capabilities": lambda request: {"capabilities": ["jobs.kind.v1"]}})
    assert run_cli(["runs", "--last", "5", "--running"]) == 0
    table = capsys.readouterr().out
    assert "RUNNING" in table and JOB in table and "astra" in table
    # C-26.12: no turns unless asked; C-25.1: the fields only because `jobs.kind.v1` was advertised.
    assert server.args("list") == {"mine": None, "running": True, "last": 5,
                                   "kind": None, "include_turns": False}
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
    monkeypatch.setattr(client_module, "identity_report",
                        lambda *a, **k: calls.append(1) or (None, "stubbed"))
    probe = client_module.Client(root)
    for _ in range(4):
        probe.check_available()
    assert len(calls) == 1


def test_boot_id_is_read_once(monkeypatch):
    """C-5.3 the stable boot-session UUID can be cached."""
    from subfleet import client as client_module
    monkeypatch.setattr(client_module, "_BOOT_ID", [])
    reads: list[int] = []
    monkeypatch.setattr(client_module, "_read_boot_id",
                        lambda: reads.append(1) or "66355737-51db-46d4-8f31-c928bc955e16")
    assert client_module.boot_id() == "66355737-51db-46d4-8f31-c928bc955e16"
    assert client_module.boot_id() == "66355737-51db-46d4-8f31-c928bc955e16"
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


def test_staged_prompts_are_private_and_pruned(daemon, root, workdir, capsys):
    """C-2.1, C-2.3 inline prompt text is staged in the state root, 0600, and pruned."""
    import time as _time
    daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "-d", "hi"]) == 0
    staged = list((root / "inbox").glob("*.md"))
    assert len(staged) == 1
    assert oct(staged[0].stat().st_mode)[-3:] == "600"
    assert oct((root / "inbox").stat().st_mode)[-3:] == "700"
    old = root / "inbox" / "ancient.md"
    old.write_text("old\n")
    os.utime(old, (0, _time.time() - cli.INBOX_KEEP_S - 60))
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "-d", "again"]) == 0
    assert not old.exists()
    capsys.readouterr()


def test_no_percentage_without_a_provider_reading(capsys):
    """C-9.1 a percentage is rendered only from a provider reading, stale marked."""
    table = cli.format_status({
        "lanes": [{"lane_id": "codex-1", "provider": "codex",
                   "account_key": "codex:a", "owner": "v2"},
                  {"lane_id": "codex-2", "provider": "codex",
                   "account_key": "codex:b", "owner": "v2"},
                  {"lane_id": "codex-3", "provider": "codex",
                   "account_key": "codex:c", "owner": "v2"}],
        "readings": [
            {"lane_id": "codex-1", "window": "five_hour", "utilization": 0.42,
             "label": "provider"},
            {"lane_id": "codex-2", "window": "five_hour", "utilization": 0.91,
             "label": "stale-provider"},
            {"lane_id": "codex-3", "window": "five_hour", "utilization": 0.77,
             "label": "admission-observed"},
        ]})
    assert "five_hour 42%" in table
    assert "five_hour 91% stale" in table
    assert "77%" not in table                 # admission-observed carries no percentage
    assert "five_hour admission-observed" in table


def test_runs_show_acknowledges_this_sessions_notices(daemon, monkeypatch, capsys):
    """C-15.3 a notice is acknowledged when its session runs `runs show <job>`."""
    job = {"job_id": JOB, "state": "succeeded", "rc": 0, "notices": [
        {"notice_id": 4, "session_id": "sess-9", "state": "offered", "text": "done"},
        {"notice_id": 5, "session_id": "sess-9", "state": "acknowledged", "text": "old"},
        {"notice_id": 6, "session_id": "other", "state": "pending", "text": "theirs"},
    ]}
    server = daemon({"show": lambda request: job,
                     "notice.ack": lambda request: {"acknowledged": 1}})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-9")
    assert run_cli(["runs", "show", JOB]) == 0
    assert server.args("notice.ack") == {"session_id": "sess-9", "notice_ids": [4]}
    capsys.readouterr()


def test_runs_show_outside_a_session_acknowledges_nothing(daemon, capsys):
    """C-15.3 acknowledgement belongs to a session; there is none to speak for here."""
    server = daemon({"show": lambda request: {
        "job_id": JOB, "state": "succeeded",
        "notices": [{"notice_id": 4, "session_id": "sess-9", "state": "offered"}]}})
    assert run_cli(["runs", "show", JOB]) == 0
    assert "notice.ack" not in server.ops()
    capsys.readouterr()


def test_a_failed_acknowledgement_does_not_change_the_show(daemon, monkeypatch, capsys):
    """C-15.3 acknowledgement is best effort; `runs show` still succeeds."""
    daemon({"show": lambda request: {
        "job_id": JOB, "state": "succeeded",
        "notices": [{"notice_id": 4, "session_id": "sess-9", "state": "offered"}]},
        "notice.ack": lambda request: protocol.fail(request.id, Exit.OPERATIONAL,
                                                    "the notice vanished")})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-9")
    assert run_cli(["runs", "show", JOB]) == 0
    assert JOB in capsys.readouterr().out


def test_wait_summary_names_the_deliverable(daemon, root, capsys):
    """C-17.4 the wait summary points at the artifact `runs show --out` prints."""
    daemon({"wait": lambda request: {"jobs": {JOB: {
        "job_id": JOB, "state": "succeeded", "rc": 0, "model": "astra",
        "lane_id": "codex-1",
        "artifacts": [{"role": "deliverable", "path": str(root / "d.md")}]}}}})
    assert run_cli(["wait", JOB]) == 0
    assert f"out={root / 'd.md'}" in capsys.readouterr().err


def test_ping_joins_unquoted_words(daemon, monkeypatch, capsys):
    """C-17.1 `ping TEXT` takes the rest of the line as the message."""
    server = daemon({"ping": lambda request: {"delivered": True}})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    assert run_cli(["ping", "the", "build", "is", "green"]) == 0
    assert server.args("ping")["text"] == "the build is green"
    capsys.readouterr()


def test_reap_blames_the_store_not_the_daemon_when_the_daemon_is_up(daemon, capsys):
    """C-17.3 an unreadable store while the daemon runs is operational, not 69."""
    daemon({"daemon.status": lambda request: {"version": "2.0.0a0"}})
    assert run_cli(["runs", "reap"]) == 1
    captured = capsys.readouterr()
    assert "the daemon is running but its store is not readable" in captured.err
    assert "subfleet daemon start" not in captured.err


def test_usage_errors_return_two_and_help_returns_zero(capsys):
    """C-17.3 a usage error is exit 2; main() returns a code, it never raises."""
    assert run_cli(["not-a-verb"]) == 2
    capsys.readouterr()
    assert run_cli(["run", "--tier", "nope"]) == 2
    capsys.readouterr()
    assert run_cli(["--help"]) == 0
    assert "subfleet run" in capsys.readouterr().out
    assert run_cli(["--version"]) == 0
    assert capsys.readouterr().out.startswith("subfleet 2.")


@pytest.mark.parametrize("envelope", [False, True])
def test_show_out_online_prefers_the_accepted_attempt(daemon, root, capsys, envelope):
    """C-8.2, C-4.3 a job with two attempts shows the accepted one's deliverable."""
    first, second = root / "a1.md", root / "a2.md"
    first.write_text("# the failed attempt\n")
    second.write_text("# the accepted attempt\n")
    response = {
        "job_id": JOB, "state": "succeeded", "accepted_attempt_id": f"{JOB}/a2",
        "artifacts": [
            {"role": "deliverable", "path": str(first), "attempt_id": f"{JOB}/a1"},
            {"role": "deliverable", "path": str(second), "attempt_id": f"{JOB}/a2"}]}
    if envelope:
        artifacts = response.pop("artifacts")
        response = {"job": response, "artifacts": artifacts}
    daemon({"show": lambda request: response})
    assert run_cli(["runs", "show", JOB, "--out"]) == 0
    assert capsys.readouterr().out == "# the accepted attempt\n"


def test_last_zero_means_no_limit_on_both_sides(daemon, capsys):
    """C-17.1 `--last 0` is unbounded online exactly as it is offline."""
    server = daemon({"list": lambda request: {"jobs": []}})
    assert run_cli(["runs", "--last", "0"]) == 0
    assert server.args("list")["last"] is None
    capsys.readouterr()


def test_wait_keeps_polling_until_the_job_is_terminal(daemon, capsys):
    """C-15.4 `wait` loops the server-side long poll; one poll is not enough."""
    calls: list[int] = []

    def poll(request):
        calls.append(1)
        if len(calls) < 3:
            return {"timeout": True}
        return terminal("failed", rc=5)

    server = daemon({"wait": poll})
    assert run_cli(["wait", JOB]) == 5
    assert len(calls) == 3
    assert [op for op in server.ops() if op == "wait"] == ["wait"] * 3
    capsys.readouterr()


def test_wait_re_asks_for_a_job_that_is_still_running(daemon, capsys):
    """C-15.4 a non-terminal state in a poll answer keeps the job pending."""
    calls: list[int] = []

    def poll(request):
        calls.append(1)
        state = "running" if len(calls) < 2 else "succeeded"
        return {"jobs": {JOB: {"job_id": JOB, "state": state, "rc": 0}}}

    daemon({"wait": poll})
    assert run_cli(["wait", JOB]) == 0
    assert len(calls) == 2
    capsys.readouterr()


def test_wait_over_several_jobs_returns_the_worst_code(daemon, capsys):
    """C-17.3 `wait a b c` returns the worst of the mapped codes."""
    other, third = "20260905-120001-two", "20260905-120002-three"
    daemon({"wait": lambda request: {"jobs": {
        JOB: {"job_id": JOB, "state": "succeeded", "rc": 0},
        other: {"job_id": other, "state": "failed", "rc": 3},
        third: {"job_id": third, "state": "cancelled"}}}})
    assert run_cli(["wait", JOB, other, third]) == 130
    summaries = capsys.readouterr().err
    assert all(job in summaries for job in (JOB, other, third))


def test_wait_partial_timeout_reports_each_job(daemon, capsys):
    """C-17.3 a job still running at the deadline is 124 beside its finished peers."""
    other = "20260905-120001-two"
    daemon({"wait": lambda request: {"jobs": {
        JOB: {"job_id": JOB, "state": "succeeded", "rc": 0},
        other: {"job_id": other, "state": "running"}}}})
    assert run_cli(["wait", JOB, other, "--timeout", "1"]) == 124
    captured = capsys.readouterr().err
    assert f"{other} still running" in captured and f"{JOB} SUCCEEDED" in captured


def test_kill_over_several_jobs_returns_the_worst_code(daemon, capsys):
    """C-17.3 `kill a b` reports each job and returns the worst code."""
    other = "20260905-120001-two"

    def killer(request):
        if request.args["job_id"] == other:
            return protocol.fail(request.id, Exit.INVALID_INPUT, "no such job")
        return {"status": "cancel requested"}

    server = daemon({"kill": killer})
    assert run_cli(["kill", JOB, other]) == 2
    assert [r.args["job_id"] for r in server.requests if r.op == "kill"] == [JOB, other]
    captured = capsys.readouterr()
    assert f"{JOB} cancel requested" in captured.out
    assert "no such job" in captured.err


def test_run_wait_inside_a_claude_session_still_blocks(daemon, monkeypatch, capsys,
                                                       workdir):
    """C-17.6 `--wait` inside a session keeps the job detached but waits inline."""
    calls: list[int] = []

    def poll(request):
        calls.append(1)
        return terminal("failed", rc=6)

    daemon({"submit": submit_ok, "wait": poll})
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-9")
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "--wait", "hi"]) == 6
    assert calls == [1]
    captured = capsys.readouterr()
    assert captured.out.strip() == JOB
    assert "waiting inline" in captured.err
    assert "--attach waits inline" in captured.err


# --- fixes from the adversarial review ----------------------------------------

def test_wait_mine_that_never_resolves_a_job_times_out(daemon, monkeypatch, capsys):
    """C-17.3 `wait --mine --timeout` expiring with no job named is still 124."""
    daemon({"wait": lambda request: {"timeout": True}})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    assert run_cli(["wait", "--mine", "--timeout", "1"]) == 124
    assert "nothing reached a terminal state" in capsys.readouterr().err


def test_wait_timeout_holds_even_when_the_daemon_wedges(daemon, capsys):
    """C-15.4, C-17.3 --timeout is a wall-clock bound, not a per-poll one."""
    import time as _time

    def wedged(request):
        _time.sleep(5)
        return {"timeout": True}

    daemon({"wait": wedged})
    started = _time.monotonic()
    assert run_cli(["wait", JOB, "--timeout", "1"]) == 124
    assert _time.monotonic() - started < 4.5
    capsys.readouterr()


def test_a_daemon_code_outside_the_table_becomes_one(daemon, capsys, workdir):
    """C-17.3 every exit code has one meaning; 256 would reach the shell as 0."""
    daemon({"submit": lambda request: protocol.fail(request.id, 256, "boom")})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 1
    captured = capsys.readouterr()
    assert "boom" in captured.err and "256" in captured.err


def test_a_daemon_failure_numbered_zero_is_not_success(daemon, capsys, workdir):
    """C-17.3 an `ok: false` answer can never exit 0."""
    daemon({"submit": lambda request: protocol.fail(request.id, 0, "refused")})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 1
    capsys.readouterr()


@pytest.mark.parametrize("body", [b"[1, 2, 3]\n", b'"a string"\n', b"null\n",
                                  b'{"v": 1, "ok": false, "error": 7}\n'])
def test_a_wrong_shaped_response_is_exit_one(daemon, capsys, workdir, body):
    """C-16.1 valid JSON of the wrong shape is an exit code, not a traceback."""
    daemon({"submit": lambda request: body})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 1
    assert "malformed response" in capsys.readouterr().err


def test_a_response_from_another_protocol_version_is_refused(daemon, capsys, workdir):
    """C-16.1 the version is part of the wire contract in both directions."""
    daemon({"submit": lambda request: b'{"v": 2, "ok": true, "result": {}}\n'})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 1
    assert "protocol version 2" in capsys.readouterr().err


def test_an_unclosed_response_line_does_not_hang(daemon, capsys, workdir):
    """C-16.1 a peer that never sends a newline is bounded by the timeout."""
    daemon({"submit": lambda request: b'{"v": 1, "ok": true'})   # no newline
    import time as _time
    started = _time.monotonic()
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 1
    assert _time.monotonic() - started < 30
    capsys.readouterr()


def test_a_request_id_can_never_name_a_path(daemon, root, workdir, capsys):
    """C-2.1, C-1.5 a caller-supplied request id stages inside the state root."""
    daemon({"submit": submit_ok, "wait": lambda request: terminal("succeeded", rc=0)})
    victim = root / "victim.md"
    victim.write_text("do not truncate me\n")
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "-d",
                    "--request-id", "../victim", "hi"]) == 0
    assert victim.read_text() == "do not truncate me\n"
    staged = list((root / "inbox").glob("*.md"))
    assert len(staged) == 1 and staged[0].parent == root / "inbox"
    capsys.readouterr()


def test_the_request_id_reaches_the_wire_unchanged(daemon, root, workdir, capsys):
    """C-1.5 a request id is the caller's string on the wire, whatever it looks like."""
    server = daemon({"submit": submit_ok})
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "-d",
                    "--request-id", "../victim", "hi"]) == 0
    assert server.args("submit")["request_id"] == "../victim"
    capsys.readouterr()


@pytest.mark.parametrize("second_prompt", ["first prompt", "different prompt"])
def test_pending_submission_prompts_never_overwrite_each_other(root, second_prompt):
    """C-6.2: repeated request ids preserve both inputs until digest comparison."""
    first = cli.stage_prompt("first prompt", "same-request", root)
    second = cli.stage_prompt(second_prompt, "same-request", root)
    # The first CLI can still be waiting for the daemon to read this path.
    assert first != second
    assert first.read_text() == "first prompt\n"
    assert second.read_text() == second_prompt + "\n"
    assert first.stat().st_mode & 0o777 == second.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("argv", [
    ["run", "-m", "opus", "hello"],
    ["resume", JOB],
    ["handoff", "--last", "--to", "astra"],
])
@pytest.mark.parametrize("identity", ["", "x" * 129])
def test_invalid_request_ids_are_rejected_without_truncation(
        argv, identity, root, monkeypatch, capsys):
    """C-1.5, C-6.2: invalid caller ids cannot silently alias a different request."""
    def no_transport(*args, **kwargs):
        pytest.fail("invalid identity reached the daemon")

    monkeypatch.setattr(cli, "_client", no_transport)
    assert run_cli([*argv, "--request-id", identity]) == 2
    assert "--request-id" in capsys.readouterr().err
    assert not (root / "inbox").exists()


def test_a_relative_out_path_is_resolved_for_the_daemon(daemon, workdir, monkeypatch,
                                                        capsys):
    """C-6.1 the daemon writes -o, and its cwd is not the caller's."""
    server = daemon({"submit": submit_ok})
    monkeypatch.chdir(workdir)
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "-d", "-o", "out.md",
                    "hi"]) == 0
    assert server.args("submit")["out_path"] == str(workdir / "out.md")
    capsys.readouterr()


def test_an_unambiguous_outcome_class_supplies_the_exit_code(capsys):
    """C-17.3 the job's rc rules when it is in the table; the class fills the gap.

    A provider rc outside the table, or none at all, would otherwise collapse to
    1 and lose the two classes that name exactly one code.
    """
    assert cli.exit_for_job({"job_id": JOB, "state": "failed", "rc": 127,
                             "outcome_class": "cli-too-old"}) == 6
    assert cli.exit_for_job({"job_id": JOB, "state": "failed", "rc": None,
                             "outcome_class": "auth-dead"}) == 5
    # The rc wins whenever it is one of the table's codes (C-17.3).
    assert cli.exit_for_job({"job_id": JOB, "state": "failed", "rc": 1,
                             "outcome_class": "auth-dead"}) == 1
    # `limited` is 3 or 4 depending on the pin, so only the daemon can say.
    assert cli.exit_for_job({"job_id": JOB, "state": "failed", "rc": 3,
                             "outcome_class": "limited"}) == 3
    assert cli.exit_for_job({"job_id": JOB, "state": "failed", "rc": 99,
                             "outcome_class": "limited"}) == 1
    assert cli.exit_for_job({"job_id": JOB, "state": "succeeded", "rc": 0,
                             "outcome_class": "ok"}) == 0
    capsys.readouterr()


def test_show_json_with_out_is_a_usage_error(daemon, capsys):
    """C-17.1 --json is the metadata object; it cannot also stream an artifact."""
    daemon({"show": lambda request: {"job_id": JOB, "state": "succeeded"}})
    assert run_cli(["runs", "show", JOB, "--out", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "cannot also stream" in captured.err


def test_resume_without_a_job_id_is_an_operational_error(daemon, root, capsys):
    """C-17.3 an empty job id on stdout would be a lie about what was created."""
    daemon({"show": lambda request: {"job_id": JOB, "workdir": str(root)},
            "submit": lambda request: {"created": False}})
    assert run_cli(["resume", JOB]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "returned no job id" in captured.err


def test_formatters_survive_numbers_that_are_not_numbers(capsys):
    """C-17.4 a scalar of the wrong type renders, it does not raise."""
    table = cli.format_runs([{"job_id": JOB, "state": "succeeded", "rc": "0",
                              "out_bytes": "lots", "duration_s": "a while"}])
    assert JOB in table
    status = cli.format_status({
        "lanes": [{"lane_id": "codex-1", "in_flight": "two"}],
        "readings": [{"lane_id": "codex-1", "window": "five_hour",
                      "utilization": "high", "label": "provider"}]})
    assert "codex-1" in status and "?" in status
    capsys.readouterr()


def test_the_offline_banner_is_prose_on_stderr(root, capsys):
    """C-17.4 stdout carries the table; the offline banner is prose."""
    from test_offline import build_store
    build_store(root)
    assert run_cli(["status"]) == 0
    captured = capsys.readouterr()
    assert "offline" in captured.err and "offline" not in captured.out


def test_wait_ignores_jobs_the_caller_did_not_ask_about(daemon, capsys):
    """C-17.3 a daemon that names another job cannot change this call's exit code."""
    other = "20260905-999999-not-mine"
    daemon({"wait": lambda request: {"jobs": {
        JOB: {"job_id": JOB, "state": "succeeded", "rc": 0},
        other: {"job_id": other, "state": "failed", "rc": 7}}}})
    assert run_cli(["wait", JOB]) == 0
    captured = capsys.readouterr().err
    assert JOB in captured and other not in captured


def test_wait_mine_does_adopt_what_the_daemon_names(daemon, monkeypatch, capsys):
    """C-17.1 `--mine` is a resolver: the daemon decides which jobs are in scope."""
    other = "20260905-999999-mine-too"
    daemon({"wait": lambda request: {"jobs": {
        other: {"job_id": other, "state": "failed", "rc": 7}}}})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    assert run_cli(["wait", "--mine"]) == 7
    assert other in capsys.readouterr().err


def test_wait_backs_off_when_the_long_poll_returns_at_once(daemon, capsys):
    """C-15.4, C-16.4 an immediate long poll must not become a five-per-second loop."""
    import time as _time
    calls: list[float] = []

    def poll(request):
        calls.append(_time.monotonic())
        return {"timeout": True}

    daemon({"wait": poll})
    assert run_cli(["wait", JOB, "--timeout", "2"]) == 124
    assert len(calls) <= 6, f"{len(calls)} polls in two seconds"
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert gaps == sorted(gaps)              # each pause is at least the last
    capsys.readouterr()


def test_resume_carries_the_source_routing_and_its_lane(daemon, root, capsys):
    """C-6.1, C-12.1 a resume keeps the source job's routing and its own lane."""
    server = daemon({
        "show": lambda request: {
            "job_id": JOB, "workdir": str(root), "sandbox": "workspace-write",
            "task": "build", "tier": "hard", "pinned_model": "astra",
            "exclusions": ["a@b.c"], "accepted_attempt_id": f"{JOB}/a2",
            "attempts": [{"attempt_id": f"{JOB}/a1", "lane_id": "codex-1"},
                         {"attempt_id": f"{JOB}/a2", "lane_id": "codex-9"}]},
        "submit": submit_ok})
    assert run_cli(["resume", JOB]) == 0
    args = server.args("submit")
    assert args["pinned_lane"] == "codex-9"          # the accepted attempt's lane
    assert args["task"] == "build" and args["tier"] == "hard"
    assert args["pinned_model"] == "astra" and args["exclusions"] == ["a@b.c"]
    assert args["sandbox"] == "workspace-write"
    capsys.readouterr()


def test_resume_unwraps_daemon_show_and_keeps_actual_workspace(daemon, root, capsys):
    """C-12.3/4: the real show envelope must resume the source lane and worktree."""
    server = daemon({
        "show": lambda request: {
            "job": {"job_id": JOB, "workdir": str(root / "source"),
                    "worktree": str(root / "allocated"), "sandbox": "workspace-write",
                    "state": "cancelled", "task": "build", "tier": "hard",
                    "exclusions": '["excluded@example.com"]',
                    "accepted_attempt_id": f"{JOB}/a2"},
            "attempts": [{"attempt_id": f"{JOB}/a1", "lane_id": "codex-1"},
                         {"attempt_id": f"{JOB}/a2", "lane_id": "codex-9"}]},
        "submit": submit_ok})
    assert run_cli(["resume", JOB]) == 0
    args = server.args("submit")
    assert args["workdir"] == str(root / "allocated")
    assert args["pinned_lane"] == "codex-9"
    assert args["sandbox"] == "workspace-write"
    assert args["exclusions"] == ["excluded@example.com"]
    assert args["parent_job_id"] == JOB and args["independent"] is True
    capsys.readouterr()


@pytest.mark.parametrize("envelope", [False, True])
def test_resume_never_inherits_unmeasured_reserve_authorization(daemon, root, envelope):
    source = {"job_id": JOB, "workdir": str(root), "sandbox": "read-only",
              "pinned_model": "fable", "pinned_lane": "claude-1",
              "unmeasured_reserve_reason": "Authorized only for the previous job."}
    server = daemon({"show": lambda request: {"job": source} if envelope else source,
                     "submit": submit_ok})
    assert run_cli(["resume", JOB]) == 0
    assert server.args("submit")["pinned_model"] == "fable"
    assert server.args("submit")["pinned_lane"] == "claude-1"
    assert server.args("submit")["unmeasured_reserve_reason"] is None


def test_resume_falls_back_to_the_job_rows_lane(daemon, root, capsys):
    """C-12.1 the lane may arrive on the job row instead of an attempt."""
    server = daemon({"show": lambda request: {"job_id": JOB, "workdir": str(root),
                                              "lane_id": "claude-3"},
                     "submit": submit_ok})
    assert run_cli(["resume", JOB]) == 0
    assert server.args("submit")["pinned_lane"] == "claude-3"
    capsys.readouterr()


def test_a_lane_id_that_is_not_a_string_still_renders(capsys):
    """C-16.2 a daemon field of the wrong type must not be used as a raw dict key."""
    table = cli.format_status({
        "lanes": [{"lane_id": ["odd"], "provider": "codex"}],
        "readings": [{"lane_id": ["odd"], "window": "five_hour",
                      "utilization": 0.5, "label": "provider"}]})
    assert "50%" in table
    capsys.readouterr()


def test_an_identity_mismatch_names_the_rendering_trap(root, capsys, workdir):
    """C-5.3 a start time that differs is reported with why, not just as dead.

    The recorded value is rendered by whoever wrote it, so a mismatch is either
    a reused pid or two sides rendering `lstart` in different locales; the
    message has to let a reader tell those apart.
    """
    (root / "daemon.lock").write_text(json.dumps(
        {"pid": os.getpid(), "proc_start": "Sat Jan  1 00:00:00 2000"}))
    assert run_cli(["run", "-m", "opus", "-C", str(workdir), "hi"]) == 69
    captured = capsys.readouterr().err
    assert "not 'Sat Jan  1 00:00:00 2000' as recorded" in captured
    assert "LC_ALL=C and TZ=UTC" in captured


def test_run_against_a_daemon_older_than_policy_sandboxes_says_restart(daemon, root, capsys, workdir):
    """Review of d261: a daemon that cannot read `policy` is a version signal."""
    daemon({"submit": lambda request: protocol.fail(request.id, Exit.INVALID_INPUT,
                                                    "'policy' is not a valid Sandbox")})
    assert run_cli(["run", "--task", "build", "--tier", "standard", "-C", str(workdir), "hi"]) == 69
    err = capsys.readouterr().err
    assert "daemon restart" in err and "-s read-only" in err
