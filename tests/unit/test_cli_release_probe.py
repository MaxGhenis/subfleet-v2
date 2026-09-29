"""C-5.7a, C-17.1: `lanes release-probe`, and quarantined probes in `status`, `why` and `kill`.

The CLI against a fake daemon (tests/unit/conftest.py). The daemon side, and the
property that nothing releases a probe's lease but a verified-empty census or a
recorded override, are in tests/fake/test_probe_operator_override.py.
"""

from __future__ import annotations

import json

import pytest

from subfleet import cli, render
from subfleet.contracts import Exit

JOB = "20260927-055500-cs-research-g01"
HOLDER = "probe:4f2a"
TIMER = "probe:timer:9b1c"

QUARANTINED = {
    "holder": HOLDER, "lane_id": "codex-3", "lane_ids": ["codex-3"], "job_id": JOB, "kind": "admission",
    "state": "quarantined", "created_at": "2026-09-27T09:57:40Z", "recorded_at": "2026-09-27T09:58:02Z",
    "live_pids": [], "unverifiable": True, "errors": ["marker enumeration unavailable"],
    "containment": {"live_pids": [], "unverifiable": True, "errors": ["marker enumeration unavailable"]},
    "looks": 6, "next_look_in_s": 41.5, "requested": None, "operator_look": None,
    "resolve": render.probe_resolutions("codex-3", JOB)}


def run_cli(argv: list[str]) -> int:
    return cli.main(argv)


def requested(request, **extra) -> dict:
    return {"release_probe": {"holder": TIMER, "lane_id": "codex-3", "lane_ids": ["codex-3"],
                              "job_id": None, "kind": "keepalive", "state": "quarantined",
                              "status": "resolution requested", "since_event": 100,
                              "mode": "force-release" if request.args.get("force_release") else "confirm-dead",
                              "requested_at": "2026-09-27T12:00:00Z", **extra}}


def lanes(probes):
    """A daemon whose `release-probe` is accepted and whose `lanes` listing holds `probes`."""
    def answer(request):
        if request.args.get("action") == "release-probe":
            return requested(request)
        return {"lanes": [], "leases": [], "probes": probes}
    return answer


# --- the verb -------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["lanes", "release-probe", "codex-3"],
    ["lanes", "release-probe", TIMER, "--force-release", "--note", "ps is failing"],
    ["lanes", "release-probe", "codex-3", "--confirm-dead", "--wait", "--timeout", "5", "--json"],
])
def test_c17_1_release_probe_parses_to_the_lanes_handler(argv):
    assert cli.build_parser().parse_args(argv).handler.__name__ == "cmd_lanes"


def test_c5_7a_the_two_resolutions_are_exclusive(capsys):
    with pytest.raises(SystemExit) as refused:
        cli.build_parser().parse_args(["lanes", "release-probe", "codex-3", "--confirm-dead", "--force-release"])
    assert refused.value.code == 2
    capsys.readouterr()


def test_c5_7a_release_probe_reaches_the_lanes_op_and_says_what_was_asked(daemon, capsys):
    server = daemon({"lanes": lanes([])})
    assert run_cli(["lanes", "release-probe", "codex-3"]) == 0
    sent = server.args("lanes")
    assert (sent["action"], sent["lane_id"], sent["force_release"], sent["operator_note"]) == (
        "release-probe", "codex-3", False, None)
    captured = capsys.readouterr()
    assert captured.out == f"{TIMER} on codex-3: --confirm-dead requested\n"
    assert "the next admission pass acts on it" in captured.err
    assert run_cli(["lanes", "release-probe", TIMER, "--force-release", "--note", "ps is failing"]) == 0
    sent = server.args("lanes")
    assert (sent["lane_id"], sent["force_release"], sent["operator_note"]) == (TIMER, True, "ps is failing")
    assert capsys.readouterr().out == f"{TIMER} on codex-3: --force-release requested\n"


@pytest.mark.parametrize("answer,line", [
    ({"lane_id": "codex-3", "holder": None, "status": "no probe"}, "codex-3: no probe holds a lane slot"),
    ({"holder": TIMER, "lane_id": "codex-3", "state": "completed", "status": "no probe"},
     f"codex-3: no probe holds a lane slot ({TIMER} is completed)"),
    ({"holder": HOLDER, "lane_id": "codex-3", "lane_ids": ["codex-3"], "state": "starting",
      "status": "not quarantined"}, f"{HOLDER} on codex-3: not quarantined (starting); nothing to resolve"),
])
def test_c5_7a_nothing_to_resolve_is_said_and_is_not_an_error(daemon, capsys, answer, line):
    daemon({"lanes": lambda request: {"release_probe": answer}})
    assert run_cli(["lanes", "release-probe", "codex-3"]) == 0
    assert capsys.readouterr().out == line + "\n"


def test_c5_7a_an_older_daemon_is_not_reported_as_having_resolved_anything(daemon, capsys):
    """C-16.2: a daemon that predates the action ignores it and lists the lanes."""
    daemon({"lanes": lambda request: {"lanes": [], "leases": []}})
    assert run_cli(["lanes", "release-probe", "codex-3"]) == int(Exit.DAEMON_UNAVAILABLE)
    assert "older than this CLI" in capsys.readouterr().err


def test_c5_7a_there_is_no_offline_resolution(root, capsys):
    """C-3.4, C-17.5: releasing a lease and recording an event are the daemon's."""
    assert run_cli(["lanes", "release-probe", "codex-3", "--force-release"]) == int(Exit.DAEMON_UNAVAILABLE)
    assert "only the daemon does" in capsys.readouterr().err


def test_c5_7a_json_is_one_object(daemon, capsys):
    daemon({"lanes": lanes([])})
    assert run_cli(["lanes", "release-probe", "codex-3", "--json"]) == 0
    [line] = capsys.readouterr().out.splitlines()
    assert json.loads(line)["release_probe"]["status"] == "resolution requested"


# --- --wait ---------------------------------------------------------------------------

def test_c5_7a_wait_reports_a_release(daemon, capsys):
    daemon({"lanes": lanes([])})                            # the holder no longer holds a slot
    assert run_cli(["lanes", "release-probe", "codex-3", "--wait"]) == 0
    assert capsys.readouterr().out == f"{TIMER} on codex-3: released (it no longer holds a lane slot)\n"
    assert run_cli(["lanes", "release-probe", "codex-3", "--wait", "--force-release"]) == 0
    # Not "the override is recorded": its own look, or another request, may be what released it.
    assert capsys.readouterr().out == f"{TIMER} on codex-3: released (it no longer holds a lane slot)\n"


def test_c5_7a_wait_reports_a_census_that_found_it_live(daemon, capsys):
    look = {"event_id": 101, "at": "2026-09-27T12:00:01Z", "operator_note": None,
            "containment": {"live_pids": [4242], "unverifiable": False}}
    daemon({"lanes": lanes([{**QUARANTINED, "holder": TIMER, "job_id": None, "kind": "keepalive",
                             "operator_look": look, "live_pids": [4242], "unverifiable": False,
                             "resolve": render.probe_resolutions("codex-3", None)}])})
    assert run_cli(["lanes", "release-probe", "codex-3", "--wait"]) == int(Exit.OPERATIONAL)
    captured = capsys.readouterr()
    assert captured.out == f"{TIMER} on codex-3: still quarantined\n"
    assert "live pids 4242" in captured.err
    assert "subfleet lanes release-probe codex-3 --force-release" in captured.err


def test_c5_7a_an_older_look_is_not_this_requests_answer(daemon, capsys):
    """A `probe.still_live` recorded before the request (event 99 < 100) is not its outcome."""
    stale = {"event_id": 99, "at": "2026-09-27T11:00:00Z", "containment": {"live_pids": [1]}}
    daemon({"lanes": lanes([{**QUARANTINED, "holder": TIMER, "operator_look": stale}])})
    assert run_cli(["lanes", "release-probe", "codex-3", "--wait", "--timeout", "0"]) == int(Exit.WAIT_TIMEOUT)
    captured = capsys.readouterr()
    assert captured.out == f"{TIMER} on codex-3: --confirm-dead requested\n"
    assert "not acted on within 0 s" in captured.err


def test_c5_7a_a_look_that_acted_on_another_request_is_not_this_ones(daemon, capsys):
    """While this --force-release waited, another operator's --confirm-dead,
    taken by the pass before it, was looked at and found the probe live. That
    look is not this command's answer: its request is still pending and the
    next pass releases the probe. A look answers only if it names this
    request's id."""
    theirs = {"event_id": 101, "at": "2026-09-27T12:00:01Z", "containment": {"live_pids": [7]},
              "requests": [{"id": "theirs", "mode": "confirm-dead", "at": "2026-09-27T12:00:00Z", "via": "kill"}]}
    shown = {"look": theirs}

    def answer(request):
        if request.args.get("action") == "release-probe":
            return requested(request, request_id="mine")
        return {"lanes": [], "leases": [], "probes": [{**QUARANTINED, "holder": TIMER, "job_id": None,
                                                       "kind": "keepalive", "operator_look": shown["look"]}]}
    daemon({"lanes": answer})
    argv = ["lanes", "release-probe", "codex-3", "--force-release", "--wait", "--timeout", "0"]
    assert run_cli(argv) == int(Exit.WAIT_TIMEOUT)
    assert capsys.readouterr().out == f"{TIMER} on codex-3: --force-release requested\n"
    shown["look"] = {**theirs, "requests": [*theirs["requests"], {"id": "mine", "mode": "force-release"}]}
    assert run_cli(argv) == int(Exit.OPERATIONAL)
    assert capsys.readouterr().out == f"{TIMER} on codex-3: still quarantined\n"
    shown["look"] = {key: value for key, value in theirs.items() if key != "requests"}   # no request ids: older
    assert run_cli(argv) == int(Exit.OPERATIONAL)
    capsys.readouterr()


def test_c5_7a_wait_finds_its_own_look_behind_a_later_requests_look(daemon, capsys):
    """Its look (event 101) came first, and another operator's request was acted
    on at the next pass (event 105) before this command polled. The newest look
    is not its answer, but its own is among the probe's recent looks."""
    looks = [{"event_id": 105, "at": "2026-09-27T12:00:02Z", "ids": ["theirs"]},
             {"event_id": 101, "at": "2026-09-27T12:00:01Z", "ids": ["mine"]}]
    shown = {"looks": looks}

    def answer(request):
        if request.args.get("action") == "release-probe":
            return requested(request, request_id="mine")
        return {"lanes": [], "leases": [], "probes": [{**QUARANTINED, "holder": TIMER, "operator_looks": shown["looks"],
                                                       "operator_look": {"event_id": 105, "requests": [{"id": "theirs"}]}}]}
    daemon({"lanes": answer})
    argv = ["lanes", "release-probe", "codex-3", "--wait", "--timeout", "0"]
    assert run_cli(argv) == int(Exit.OPERATIONAL)
    assert capsys.readouterr().out == f"{TIMER} on codex-3: still quarantined\n"
    shown["looks"] = [{"event_id": 103, "at": "2026-09-27T12:00:01Z", "ids": ["mine", "later"]}]
    assert run_cli(argv) == int(Exit.OPERATIONAL), "a look that acted on a merged request answers each of them"
    capsys.readouterr()
    shown["looks"] = looks[:1]                            # only the later request's look: not this one's
    assert run_cli(argv) == int(Exit.WAIT_TIMEOUT)
    capsys.readouterr()


def test_c5_7a_a_look_recorded_when_the_request_was_is_not_its_answer(daemon, capsys):
    """`since_event` is the newest event when the request was recorded: a look
    at that same event id was taken before it, not after."""
    same = {"event_id": 100, "at": "2026-09-27T12:00:00Z", "containment": {"live_pids": [1]}}
    daemon({"lanes": lanes([{**QUARANTINED, "holder": TIMER, "operator_look": same}])})
    assert run_cli(["lanes", "release-probe", "codex-3", "--wait", "--timeout", "0"]) == int(Exit.WAIT_TIMEOUT)
    capsys.readouterr()


def test_c5_7a_kill_wait_takes_only_its_own_requests_look(daemon, capsys):
    theirs = {"event_id": 101, "at": "2026-09-27T12:00:01Z", "containment": {"live_pids": [9]},
              "requests": [{"id": "someone-else", "mode": "confirm-dead"}]}
    daemon({
        "kill": lambda request: {"job_id": JOB, "status": "resolution requested", "since_event": 100,
                                 "probes": [{"holder": HOLDER, "lane_id": "codex-3", "lane_ids": ["codex-3"],
                                             "kind": "admission", "created_at": "2026-09-27T09:57:40Z",
                                             "request_id": "this-kill"}]},
        "lanes": lambda request: {"lanes": [], "leases": [], "probes": [{**QUARANTINED, "operator_look": theirs}]}})
    assert run_cli(["kill", JOB, "--confirm-dead", "--wait", "--timeout", "0"]) == int(Exit.WAIT_TIMEOUT)
    capsys.readouterr()


# --- where a quarantined probe is shown -----------------------------------------------

def test_c5_7a_status_lists_probes_with_the_commands_that_resolve_them():
    text = cli.format_status({"lanes": [], "probes": [
        QUARANTINED,
        {"holder": TIMER, "lane_id": "codex-4", "lane_ids": ["codex-4"], "job_id": None,
         "kind": "keepalive", "state": "starting"}]})
    lines = text.splitlines()
    at = lines.index("probes")
    assert lines[at + 1] == (f"  probe {HOLDER} (job {JOB}) holds codex-3: quarantined since 2026-09-27T09:58:02Z; "
                             "census unverifiable (marker enumeration unavailable); next look in 41.5 s")
    assert f"subfleet kill {JOB} --confirm-dead" in lines[at + 2]
    assert f"subfleet kill {JOB} --force-release" in lines[at + 3]
    assert "subfleet lanes hold codex-3 --until <time> keeps work off the lane" in lines[at + 4]
    assert lines[at + 5] == f"  probe {TIMER} (keepalive turn) holds codex-4: starting"


def test_c5_7a_why_names_a_finished_jobs_quarantined_probe():
    lines = render.why_queue({"job_id": JOB, "state": "cancelled", "probes": [QUARANTINED]})
    assert lines[0] == f"Job: {JOB} is cancelled"
    assert lines[1].startswith(f"probe {HOLDER} (job {JOB}) holds codex-3: quarantined")
    assert any(f"subfleet kill {JOB} --confirm-dead" in line for line in lines)


def test_c5_7a_a_pending_request_and_an_operator_look_are_shown():
    lines = render.probe_lines({**QUARANTINED,
                                "requested": {"mode": "force-release", "at": "2026-09-27T12:00:00Z", "via": "kill"},
                                "operator_look": {"at": "2026-09-27T11:59:00Z",
                                                  "containment": {"live_pids": [7, 8], "unverifiable": False}}})
    assert "  operator's last --confirm-dead at 2026-09-27T11:59:00Z: still quarantined (live pids 7, 8)" in lines
    assert "  --force-release requested at 2026-09-27T12:00:00Z; the next admission pass acts on it" in lines


def test_c5_7a_the_resolve_commands():
    assert render.probe_resolutions("codex-3", JOB) == [f"subfleet kill {JOB} --confirm-dead",
                                                        f"subfleet kill {JOB} --force-release"]
    assert render.probe_resolutions("codex-3", None) == ["subfleet lanes release-probe codex-3 --confirm-dead",
                                                         "subfleet lanes release-probe codex-3 --force-release"]


def test_c5_7a_kill_says_which_probes_a_resolution_reached(daemon, capsys):
    detail = (f"--confirm-dead of quarantined probe {HOLDER} on codex-3 requested; "
              f"`subfleet runs show {JOB}` shows whether it is still quarantined")
    daemon({"kill": lambda request: {"job_id": JOB, "status": "resolution requested",
                                     "probes": [{"holder": HOLDER, "lane_id": "codex-3"}], "detail": detail}})
    assert run_cli(["kill", JOB, "--confirm-dead"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{JOB} resolution requested\n"
    assert detail in captured.err


# --- the design review's findings (2026-09-27) -----------------------------------------

def test_c5_7a_one_issued_at_for_the_whole_command(daemon, capsys):
    """Minted once, so a request sent again after a lost answer (C-16.3) carries
    the same instant and reaches the same probes."""
    server = daemon({"lanes": lanes([]),
                     "kill": lambda request: {"job_id": request.args["job_id"], "status": "not quarantined",
                                              "probes": []}})
    assert run_cli(["lanes", "release-probe", "codex-3"]) == 0
    assert server.args("lanes")["issued_at"].endswith("Z")
    assert run_cli(["kill", JOB, "other-job", "--force-release"]) == 0
    sent = [request.args["issued_at"] for request in server.requests if request.op == "kill"]
    assert len(sent) == 2 and sent[0] == sent[1]
    assert run_cli(["kill", JOB]) == 0
    assert server.args("kill")["issued_at"] is None, "a plain kill resolves nothing"
    capsys.readouterr()


def test_c5_7a_a_newer_probe_on_the_lane_is_named_not_resolved(daemon, capsys):
    daemon({"lanes": lambda request: {"release_probe": {
        "holder": TIMER, "lane_id": "codex-3", "lane_ids": ["codex-3"], "state": "quarantined",
        "created_at": "2026-09-27T12:00:09Z", "status": "newer probe"}}})
    assert run_cli(["lanes", "release-probe", "codex-3", "--force-release"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{TIMER} on codex-3: started after this request was issued; not resolved by it\n"
    assert f"subfleet lanes release-probe {TIMER} --force-release names it" in captured.err


def test_c5_7a_a_confirm_dead_absorbed_by_a_pending_override_says_so(daemon, capsys):
    daemon({"lanes": lambda request: requested(
        request, mode="force-release",
        absorbed="a --force-release asked earlier is pending, and this --confirm-dead does not replace it")})
    assert run_cli(["lanes", "release-probe", "codex-3"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{TIMER} on codex-3: --confirm-dead requested\n"
    assert "does not replace it" in captured.err


def test_c5_7a_kill_wait_waits_for_the_probe_not_the_job(daemon, capsys):
    """A job held by its probe is not finished, and once released it may run for
    hours: `--wait` with a resolution waits for the probe's outcome."""
    server = daemon({
        "kill": lambda request: {"job_id": JOB, "status": "resolution requested", "since_event": 100,
                                 "probes": [{"holder": HOLDER, "lane_id": "codex-3", "lane_ids": ["codex-3"],
                                             "kind": "admission", "created_at": "2026-09-27T09:57:40Z"}]},
        "lanes": lambda request: {"lanes": [], "leases": [], "probes": []}})
    assert run_cli(["kill", JOB, "--force-release", "--wait"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [f"{JOB} resolution requested",
                                         f"{JOB} {HOLDER} on codex-3: released (it no longer holds a lane slot)"]
    assert "wait" not in server.ops(), "not the job's terminal state"


def test_c5_7a_kill_wait_reports_a_probe_still_quarantined(daemon, capsys):
    look = {"event_id": 101, "at": "2026-09-27T12:00:01Z", "containment": {"live_pids": [9], "unverifiable": False}}
    daemon({"kill": lambda request: {"job_id": JOB, "status": "resolution requested", "since_event": 100,
                                     "probes": [{"holder": HOLDER, "lane_id": "codex-3", "lane_ids": ["codex-3"]}]},
            "lanes": lambda request: {"lanes": [], "probes": [{**QUARANTINED, "operator_look": look}]}})
    assert run_cli(["kill", JOB, "--confirm-dead", "--wait"]) == int(Exit.OPERATIONAL)
    assert capsys.readouterr().out.splitlines()[-1] == f"{JOB} {HOLDER} on codex-3: still quarantined"


@pytest.mark.parametrize("status,code", [("not quarantined", Exit.DAEMON_UNAVAILABLE),
                                         ("already finished", Exit.DAEMON_UNAVAILABLE),
                                         ("resolution requested", Exit.OK)])
def test_c5_7a_kill_knows_a_daemon_that_never_looks_at_probes(daemon, capsys, status, code):
    """C-16.2: an older daemon answers from the job's attempts alone. Its "not
    quarantined" says nothing about the job's probes, so it is not reported as
    the answer; its "resolution requested" reached an attempt only, and says so."""
    daemon({"kill": lambda request: {"job_id": JOB, "status": status}})
    assert run_cli(["kill", JOB, "--confirm-dead"]) == int(code)
    err = capsys.readouterr().err
    assert "older than this CLI" in err


def test_c5_7a_lanes_list_prints_the_probes(daemon, capsys):
    daemon({"lanes": lambda request: {"lanes": [{"lane_id": "codex-3", "provider": "codex"}], "leases": [],
                                      "probes": [QUARANTINED]}})
    assert run_cli(["lanes"]) == 0
    out = capsys.readouterr().out
    assert "\nprobes\n" in out and f"subfleet kill {JOB} --confirm-dead" in out


def test_c5_7a_bare_runs_show_keeps_its_shape_and_names_a_quarantined_probe_on_stderr(daemon, capsys):
    """C-17.1: stdout is still the metadata object then `--- out.md ---`; the
    probe is in the object, and the prose about it goes to stderr (C-17.4)."""
    daemon({"show": lambda request: {"job_id": JOB, "state": "waiting", "probes": [QUARANTINED]}})
    assert run_cli(["runs", "show", JOB]) == 0
    captured = capsys.readouterr()
    shown = json.loads(captured.out.split("\n--- out.md ---")[0])
    assert shown["probes"][0]["holder"] == HOLDER
    assert f"probe {HOLDER} (job {JOB}) holds codex-3: quarantined" in captured.err


def test_c5_7a_lanes_release_says_when_it_released_no_hold_and_points_at_the_probe(daemon, capsys):
    daemon({"lanes": lambda request: {"released": "codex-3", "lanes": [], "closures": [], "holds_released": 0,
                                      "probe": {"holder": HOLDER, "state": "quarantined"}}})
    assert run_cli(["lanes", "release", "codex-3"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "release: codex-3\n"
    assert "no operator hold was open on codex-3" in captured.err
    assert "subfleet lanes release-probe codex-3" in captured.err


def test_c16_3_an_unknown_release_probe_says_how_to_check_and_repeat(daemon, capsys):
    """C-16.3: sent twice and answered neither time, the outcome is unknown, not
    refused; asked again it is a new command (a new `issued_at`, C-5.7a), so the
    operator looks at `status` first."""
    server = daemon({"lanes": lambda request: b""})
    assert run_cli(["lanes", "release-probe", "codex-3", "--force-release", "--note", "ps down"]) == int(Exit.OPERATIONAL)
    err = capsys.readouterr().err
    assert "the --force-release request may have been recorded" in err
    assert ("subfleet status lists the probes that still hold a lane slot, and which; if the one you checked "
            "still does, running subfleet lanes release-probe codex-3 --force-release --note 'ps down' again "
            "is safe") in err
    sent = [request.args for request in server.requests if request.op == "lanes"]
    assert len(sent) == 2 and sent[0] == sent[1], "the same request, the same issued_at"
