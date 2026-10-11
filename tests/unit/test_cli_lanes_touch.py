"""C-18.3, C-17.1, C-17.3, C-17.4: `subfleet lanes touch` and the weekly-clock flags,
against a fake daemon on the real socket protocol."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import re
from contextlib import redirect_stdout

import pytest

from subfleet import cli, protocol, render, status_json
from subfleet.contracts import Exit
from subfleet.timers import iso

PLAN = [
    {"lane_id": "codex-1", "home": "/h/1", "action": "touch", "reason": "not-started",
     "weekly_clock": "not-started", "resets_at": "2026-10-02T20:09:00Z", "last_touch": None,
     "next_touch_at": None},
    {"lane_id": "codex-2", "home": "/h/2", "action": "skip", "reason": "spaced",
     "weekly_clock": "not-started", "resets_at": "2026-10-02T20:09:00Z",
     "last_touch": {"at": "2026-09-25T19:40:00Z", "status": "ok", "mode": "auto", "requested_at": None},
     "next_touch_at": "2026-09-25T20:40:00Z"},
]


@pytest.mark.parametrize("argv", [
    ["lanes", "touch"], ["lanes", "touch", "--all"], ["lanes", "touch", "codex-1"],
    ["lanes", "touch", "4", "--dry-run", "--json"], ["lanes", "touch", "--no-wait", "--timeout", "5"],
])
def test_every_touch_form_parses_to_the_lanes_handler(argv):
    """C-17.1: `lanes touch [<lane>|--all] [--dry-run] [--json]` is a lanes action."""
    parsed = cli.build_parser().parse_args(cli.rewrite_aliases(argv))
    assert parsed.handler.__name__ == "cmd_lanes" and parsed.lanes_command == "touch"


def test_lanes_help_lists_touch_so_the_launchd_bridge_retires():
    """C-18.3: `~/bin/codex-window-touch` retires itself once `subfleet lanes --help` lists touch."""
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert cli.main(["lanes", "--help"]) == 0
    assert re.search(r"\btouch\b", stdout.getvalue())


def test_a_lane_and_all_together_are_invalid(daemon, capsys):
    """C-17.3: one lane or --all; both is exit 2 and nothing is sent."""
    server = daemon({})
    assert cli.main(["lanes", "touch", "codex-1", "--all"]) == Exit.INVALID_INPUT
    assert server.ops() == []
    capsys.readouterr()


def test_dry_run_prints_the_plan_and_sends_no_touch(daemon, capsys):
    """C-18.3, C-17.4: the plan table on stdout, a dry-run note on stderr, JSON on request."""
    server = daemon({"lanes": lambda request: {"touch": {
        "status": "dry-run", "dry_run": True, "plan": PLAN, "touching": ["codex-1"], "model": "gpt-5.6-luna"}}})
    assert cli.main(["lanes", "touch", "--dry-run"]) == 0
    sent = server.args("lanes")
    assert sent["action"] == "touch" and sent["dry_run"] is True and sent["lane_id"] is None
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0].split()[:4] == ["lane", "action", "reason", "weekly"]
    assert lines[1].split()[:4] == ["codex-1", "touch", "not-started", "not"]
    assert "(next 2026-09-25T20:40:00Z)" in lines[2]
    assert "dry run; nothing was touched" in captured.err
    assert cli.main(["lanes", "touch", "codex-1", "--dry-run", "--json"]) == 0
    assert server.args("lanes")["lane_id"] == "codex-1"
    assert json.loads(capsys.readouterr().out)["plan"] == PLAN


def test_a_touch_waits_for_its_result_and_maps_exit_codes(daemon, capsys):
    """C-18.3, C-16.4, C-17.3: scheduled, then collected with touch-status; 0 when every touch
    succeeded, 7 when one was refused, 1 for another failure."""
    results = {"value": [{"lane_id": "codex-1", "status": "ok", "resets_at": "2026-10-02T20:09:00Z"}]}
    polls = []

    def lanes(request):
        if request.args["action"] == "touch":
            return {"touch": {"status": "scheduled", "timer": "touch", "request_id": request.args["request_id"],
                              "touching": ["codex-1"], "model": "gpt-5.6-luna", "plan": PLAN}}
        polls.append(request.args)
        if len(polls) % 2:
            return {"touch": {"status": "running", "request_id": request.args["request_id"]}}
        return {"touch": {"status": "done", "results": results["value"]}}

    server = daemon({"lanes": lanes})
    assert cli.main(["lanes", "touch", "codex-1"]) == 0
    first = server.requests[0].args
    assert polls[-1]["action"] == "touch-status" and polls[-1]["request_id"] == first["request_id"]
    assert 1 <= polls[-1]["wait_s"] <= 25
    captured = capsys.readouterr()
    assert captured.out == "codex-1: touched · weekly reset now 2026-10-02T20:09:00Z\n"
    assert "touching codex-1 with gpt-5.6-luna" in captured.err
    results["value"] = [{"lane_id": "codex-1", "status": "refused", "detail": "guard trust preflight failed"}]
    assert cli.main(["lanes", "touch", "codex-1"]) == Exit.REFUSED
    assert "codex-1: refused (guard trust preflight failed)" in capsys.readouterr().out
    results["value"] = [{"lane_id": "codex-1", "status": "unknown", "detail": "probe ended without an exit receipt"}]
    assert cli.main(["lanes", "touch", "codex-1", "--json"]) == Exit.OPERATIONAL
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "unknown"
    results["value"] = [{"lane_id": "codex-1", "status": "auth-dead"}]
    assert cli.main(["lanes", "touch", "codex-1"]) == Exit.AUTH_DEAD
    capsys.readouterr()


def test_a_named_lane_the_daemon_ended_up_not_touching_is_not_a_success(daemon, capsys):
    """C-18.3, C-17.3: between the plan and the touch a job can take the lane; say so, exit 1."""
    def lanes(request):
        if request.args["action"] == "touch":
            return {"touch": {"status": "scheduled", "request_id": "r", "touching": ["codex-1"],
                              "model": "gpt-5.6-luna", "plan": PLAN}}
        return {"touch": {"status": "done", "results": [],
                          "plan": [{**PLAN[0], "action": "skip", "reason": "busy"}]}}

    daemon({"lanes": lanes})
    assert cli.main(["lanes", "touch", "codex-1"]) == Exit.OPERATIONAL
    assert capsys.readouterr().out == "codex-1: not touched (busy)\n"


def test_no_wait_returns_once_scheduled_and_a_busy_timer_is_an_error(daemon, capsys):
    """C-18.3: --no-wait leaves the touch to the daemon; a touch already running is exit 1."""
    answer = {"status": "scheduled"}
    server = daemon({"lanes": lambda request: {"touch": {**answer, "request_id": "r", "touching": ["codex-1"],
                                                         "plan": PLAN, "model": "gpt-5.6-luna"}}})
    assert cli.main(["lanes", "touch", "--no-wait", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "scheduled"
    assert [request.args["action"] for request in server.requests] == ["touch"]
    answer["status"] = "already-running"
    assert cli.main(["lanes", "touch"]) == Exit.OPERATIONAL
    assert "already-running" in capsys.readouterr().err


def test_a_refusal_keeps_its_code_and_fix(daemon, capsys):
    """C-17.3, C-18.3: a lane no touch may use is exit 7 with the daemon's fix."""
    daemon({"lanes": lambda request: protocol.fail(request.id, Exit.REFUSED,
                                                  "lanes touch: codex-3 is owned by v1; not touched",
                                                  "subfleet lanes transfer codex-3 --to v2")})
    assert cli.main(["lanes", "touch", "codex-3"]) == Exit.REFUSED
    err = capsys.readouterr().err
    assert "owned by v1" in err and "fix: subfleet lanes transfer codex-3 --to v2" in err


def test_an_older_daemon_that_ignores_the_action_is_not_reported_as_a_touch(daemon, capsys):
    """C-16.2: unknown fields are ignored, so an old daemon answers with the roster."""
    daemon({"lanes": lambda request: {"lanes": [], "leases": []}})
    assert cli.main(["lanes", "touch"]) == Exit.DAEMON_UNAVAILABLE
    assert "older than this CLI" in capsys.readouterr().err


# --- the flags on status, lanes list, the daemon text, and status.json -------

def unstarted(now):
    return {"lane_id": "codex-4", "scope": "account", "window": "seven_day", "utilization": 0.0,
            "resets_at": iso(now + timedelta(days=7)), "label": "provider", "source": "wham",
            "observed_at": iso(now)}


def test_status_flags_unstarted_clocks_and_counts_them():
    """C-18.3: `subfleet status` marks a lane whose clock has not started and says how to start it."""
    now = datetime.now(timezone.utc)
    lanes = [{"lane_id": "codex-4", "provider": "codex", "account_key": "codex:a", "owner": "v2",
              "weekly_clock": "not-started"},
             {"lane_id": "codex-5", "provider": "codex", "account_key": "codex:b", "owner": "v2",
              "weekly_clock": "touched", "clock_touch": {"requested_at": "2026-09-25T15:52:07Z"}},
             {"lane_id": "codex-6", "provider": "codex", "account_key": "codex:c", "owner": "v2",
              "weekly_clock": None}]
    text = cli.format_status({"lanes": lanes, "readings": []})
    assert "[clock not started]" in text and "[clock touched 15:52Z]" in text
    assert text.count("clock ") == 2
    assert "codex weekly clocks not started: 1 (codex-4) — subfleet lanes touch --all" in text
    # Offline rows carry no `weekly_clock`; the readings alone say it.
    offline = cli.format_status({"lanes": [{"lane_id": "codex-4", "provider": "codex", "owner": "v2"}],
                                 "readings": [unstarted(now)]})
    assert "[clock not started]" in offline
    assert "clock" not in cli.format_status({"lanes": [{"lane_id": "codex-4", "provider": "codex"}],
                                             "readings": [{**unstarted(now), "utilization": .2}]})


def test_lanes_list_and_the_daemon_status_text_flag_the_clock():
    """C-18.3: `lanes list` and `daemon.status` text show the same flag."""
    now = datetime.now(timezone.utc)
    lane = {"lane_id": "codex-4", "provider": "codex", "account_key": "codex:a", "owner": "v2",
            "enabled": True, "weekly_clock": "not-started", "readings": [unstarted(now)], "closures": []}
    listed = cli._format_lanes({"lanes": [lane]})
    assert "[clock not started]" in listed and "weekly clocks not started: 1 (codex-4)" in listed
    assert "clock-not-started" in render.status({"lanes": [lane], "now": iso(now)})


def test_status_json_carries_the_clock_for_the_menu_bar():
    """C-18.1, C-18.3: codex homes carry `weekly_clock` and v1's `window_unstarted`; the fleet lists them."""
    now = datetime.now(timezone.utc)
    lanes = [{"lane_id": "codex-4", "provider": "codex", "account_key": "codex:a", "owner": "v2",
              "enabled": True, "home": "/h/4", "weekly_clock": "not-started", "readings": [unstarted(now)],
              "closures": []},
             {"lane_id": "codex-5", "provider": "codex", "account_key": "codex:b", "owner": "v2",
              "enabled": True, "home": "/h/5", "weekly_clock": None, "readings": [], "closures": []}]
    built = status_json.build_status({"lanes": lanes, "now": iso(now)})
    homes = {home["lane_id"]: home for home in built["codex"]["homes"]}
    assert homes["codex-4"]["window_unstarted"] is True and homes["codex-4"]["weekly_clock"] == "not-started"
    assert homes["codex-5"]["window_unstarted"] is False
    assert built["codex"]["fleet"]["unstarted"] == ["/h/4"]
