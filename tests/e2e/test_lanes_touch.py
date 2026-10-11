"""C-18.3 end to end: the daemon starts idle Codex weekly clocks through its own
supervised launch path (the guardian, the guard preflight, the API-key scrub,
the lane lease), and `subfleet lanes touch` does the same on request.

The real `subfleetd`, CLI, Codex adapter, and guard preflight run here; only the
provider binary (`tests/bin/codex`) and the usage endpoint (`tests/fake/codex_http`)
are fakes, and with `SUBFLEET_FAKE_WHAM_CLOCKS` they behave as a ChatGPT account's
weekly window does: 0% with a sliding reset until the first metered request.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def calls(root: Path, account: str) -> list[dict]:
    path = root / "clocks" / f"{account}.calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def touch_events(e2e, lane_id=None):
    rows = e2e.rows("SELECT lane_id,data_json FROM events WHERE kind='timer.touch' ORDER BY event_id")
    return [json.loads(row["data_json"]) for row in rows
            if row["data_json"] != "{}" and (lane_id is None or row["lane_id"] == lane_id)]


def test_daemon_starts_idle_weekly_clocks_through_the_supervised_path(e2e):
    """C-18.3, C-5.1, C-14.2, C-6.5, C-8.4, C-17.1: an unstarted lane is touched with one
    guarded Luna turn per lane, re-probed, recorded, and announced; the CLI forms work."""
    clocks = e2e.root / "clocks"
    clocks.mkdir()
    e2e.policy_update(lambda policy: policy.setdefault("timers", {}).update(probe_interval_s=.5))
    e2e.start(env={"SUBFLEET_FAKE_WHAM_CLOCKS": str(clocks)})

    for account in ("fake-1", "fake-2"):
        e2e.until(lambda: (clocks / f"{account}.started").exists(), timeout=30)
    e2e.until(lambda: {event["lane_id"] for event in touch_events(e2e)
                       if event["status"] == "ok"} == {"codex-1", "codex-2"}, timeout=30)

    for account in ("fake-1", "fake-2"):
        [touch] = calls(e2e.root, account)
        argv = touch["argv"]
        assert argv[:2] == ["exec", "--json"]
        assert argv[argv.index("-m") + 1] == "gpt-5.6-luna"                 # never Spark
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert "--skip-git-repo-check" in argv                               # private probe dir
        hooks = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c" and argv[i + 1].startswith("hooks=")]
        assert hooks, "the guard override from the trust preflight must be on the touch"
        assert touch["probe"] == "1"
        assert touch["env"]["CODEX_API_KEY"] is None and touch["env"]["OPENAI_API_KEY"] is None
        assert touch["env"]["CODEX_HOME"] == str(e2e.root / f"codex-{account[-1]}")
        assert "/lanes/codex-" in touch["cwd"] and "/probes/" in touch["cwd"]

    for lane_id in ("codex-1", "codex-2"):
        done = [event for event in touch_events(e2e, lane_id) if event["status"] == "ok"][-1]
        assert done["mode"] == "auto" and done["model"] == "gpt-5.6-luna"
        assert done["before"]["weekly_clock"] == "not-started" and done["requested_at"]
    log = (e2e.root / "daemon.log").read_text()
    assert re.search(r"guard preflight \S+ lane=codex-1 attempt=probes/\S+ .* ok=True", log)
    assert re.search(r"lane touch \S+ lane=codex-1 mode=auto model=gpt-5.6-luna status=ok", log)
    assert e2e.rows("SELECT * FROM jobs") == []                               # C-8.4: not a job
    e2e.until(lambda: not e2e.rows("SELECT * FROM leases WHERE lease_key LIKE 'lane:codex-%'"))
    notices = [row["text"] for row in e2e.rows("SELECT text FROM service_notices")]
    started = [int(match.group(1)) for text in notices
               if (match := re.match(r"codex: weekly clock started on (\d+) lane\(s\)", text))]
    assert sum(started) == 2, notices

    # The started clocks read as started, everywhere they are shown.
    status = e2e.until(lambda: (lambda value: value if all(
        lane.get("weekly_clock") is None and lane["readings"] for lane in value["lanes"]
        if lane["provider"] == "codex") else None)(json.loads(e2e.cli("status", "--json").stdout)))
    assert not [lane for lane in status["lanes"] if lane.get("clock_alert")]
    listed = e2e.cli("lanes", "list")
    assert listed.rc == 0 and "clock not started" not in listed.stdout
    menu = json.loads((e2e.root / "status.json").read_text())
    assert menu["codex"]["fleet"]["unstarted"] == []

    # `lanes touch` forms.
    assert re.search(r"\btouch\b", e2e.cli("lanes", "--help").stdout)       # retires the launchd bridge
    dry = e2e.cli("lanes", "touch", "--dry-run", "--json")
    assert dry.rc == 0, dry
    plan = {row["lane_id"]: row for row in json.loads(dry.stdout)["plan"]}
    assert {plan["codex-1"]["action"], plan["codex-2"]["action"]} == {"skip"}
    assert len(calls(e2e.root, "fake-1")) == 1                                # a dry run touches nothing
    nothing = e2e.cli("lanes", "touch", "--all")
    assert nothing.rc == 0 and "no lane needs a touch" in nothing.stderr
    forced = e2e.cli("lanes", "touch", "1", "--json", timeout=60)
    assert forced.rc == 0, forced
    result = json.loads(forced.stdout)
    assert result["status"] == "done" and [row["status"] for row in result["results"]] == ["ok"]
    assert result["results"][0]["mode"] == "operator" and result["results"][0]["reason"] == "forced"
    assert len(calls(e2e.root, "fake-1")) == 2 and len(calls(e2e.root, "fake-2")) == 1
    human = e2e.cli("lanes", "touch", "codex-2", timeout=60)
    assert human.rc == 0, human
    assert re.fullmatch(r"codex-2: touched · weekly reset now \S+Z\n", human.stdout)
    assert e2e.cli("lanes", "touch", "codex-1", "--all").rc == 2
    unknown = e2e.cli("lanes", "touch", "claude-1")
    assert unknown.rc == 2 and "unknown codex lane" in unknown.stderr
    assert e2e.rows("SELECT * FROM jobs") == []


def test_an_api_key_home_is_refused_before_any_touch_reaches_the_provider(e2e):
    """C-18.3, C-6.5, C-10.2: the touch goes through the daemon's API-key refusal; nothing launches."""
    clocks = e2e.root / "clocks"
    clocks.mkdir()
    home = e2e.root / "codex-1"
    auth = json.loads((home / "auth.json").read_text())
    auth["OPENAI_API_KEY"] = "sk-fixture-must-refuse"
    (home / "auth.json").write_text(json.dumps(auth))
    # No probe cycle runs during this test, so the named lane has no verdict yet
    # and the refusal comes from the touch's own launch path, not from the plan.
    e2e.policy_update(lambda policy: policy.setdefault("timers", {}).update(probe_interval_s=3600))
    e2e.start(env={"SUBFLEET_FAKE_WHAM_CLOCKS": str(clocks)})
    refused = e2e.cli("lanes", "touch", "codex-1", "--json", timeout=60)
    assert refused.rc == 7, refused
    [row] = json.loads(refused.stdout)["results"]
    assert row["status"] == "refused" and "API-key home refused" in row["detail"]
    assert calls(e2e.root, "fake-1") == [] and not (clocks / "fake-1.started").exists()
    assert e2e.rows("SELECT * FROM jobs") == []
    probes = e2e.root / "lanes" / "codex-1" / "probes"
    assert not probes.exists() or list(probes.iterdir()) == []            # a refused turn leaves nothing
    assert not e2e.rows("SELECT * FROM leases")
