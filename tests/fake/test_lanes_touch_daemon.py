"""C-18.3 in the daemon: a touch holds the lane like a probe, counts toward the fleet
cap, runs the touch model through `_timer_turn`, and `lanes touch` refuses what no
touch may use. Provider processes are replaced at `_execute_probe`, the daemon's
process seam; the end-to-end suite runs the real guardian (tests/e2e/test_lanes_touch.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile

import pytest

from subfleet import capacity, protocol, scheduler
from subfleet.contracts import (Credential, Exit, Lane, LaneOwner, Outcome, OutcomeClass, Reading,
                                ReadingLabel)
from subfleet.daemon import Daemon
from subfleet.timers import iso

WEEK = timedelta(days=7)


class Wham:
    """Every lane reads as a window that has not started, until `started` names it."""

    def __init__(self):
        self.started = {}

    def probe_status(self, lane, env):
        now = datetime.now(timezone.utc)
        start = self.started.get(lane.lane_id)
        weekly = (Reading(lane.lane_id, "account", "seven_day", 0.0, iso(now + WEEK), ReadingLabel.PROVIDER, "wham", iso(now))
                  if start is None else
                  Reading(lane.lane_id, "account", "seven_day", .01, iso(start + WEEK), ReadingLabel.PROVIDER, "wham", iso(now)))
        return {"status": "ok", "limit_reached": False, "allowed": True, "account_key": lane.account_key,
                "readings": (weekly,)}


@pytest.fixture
def daemon(monkeypatch):
    monkeypatch.setattr("subfleet.daemon.procs.boot_id", lambda: "fake-boot")
    monkeypatch.setattr("subfleet.daemon.procs.proc_start", lambda pid: "fake-start")
    policy = json.loads(Path("subfleet/default_policy.json").read_text())
    policy["caps"]["max_active_attempts"] = 1
    policy["alerts"]["operator_session"] = "test-operator"
    with tempfile.TemporaryDirectory(prefix="sft-", dir="/tmp") as temporary:
        root = Path(temporary)
        (root / "policy.json").write_text(json.dumps(policy))
        value = Daemon(root, tick_s=.01, term_grace_s=.01, desktop_prober=lambda: None)
        wham = Wham()
        value.timers.adapter_factory = lambda provider: wham
        value.wham = wham
        yield value
        value.close()


def codex(daemon, number, **changes):
    home = daemon.root / f"codex-{number}"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"tokens": {"account_id": str(number), "access_token": "FAKE-ONLY"}}))
    fields = {"owner": LaneOwner.V2, "desktop": False, "enabled": True, **changes}
    lane = Lane(f"codex-{number}", "codex", f"codex:{number}", Credential("codex", str(home), "home"), str(home),
                fields["owner"], fields["desktop"], fields["enabled"])
    daemon.store.put_lane(lane)
    return lane


def measure(daemon, lane):
    """One probe verdict, as a probe cycle would persist it."""
    daemon.timers._persist(lane, {**daemon.wham.probe_status(lane, {}), "probed_at": iso(datetime.now(timezone.utc))})


def test_a_touch_holds_its_lane_and_counts_toward_the_fleet_cap(daemon, monkeypatch):
    """C-18.3, C-6.4, C-6.10, C-11.4: during the turn the lane is leased to a probe holder,
    admission rejects it (`no-slot`), and the reservation fills the fleet's only slot."""
    touched, other = codex(daemon, 1), codex(daemon, 2)
    for lane in (touched, other):
        measure(daemon, lane)
    seen = {}

    def execute(job, lane, model, holder):
        view = daemon._capacity_view()
        seen.update(model=model["id"], kind=job["kind"], sandbox=job["sandbox"], holder=holder,
                    lease=daemon.store.one("SELECT holder FROM leases WHERE lease_key=?",
                                           (f"lane:{lane.lane_id}:slot:0",))["holder"],
                    reserved=view["reserved_probes"], unavailable=dict(view["unavailable_lanes"]),
                    open=capacity.open_lanes(view, daemon.policy["caps"]))
        for pin, key in ((touched.lane_id, "pinned"), (other.lane_id, "fleet")):
            decision = scheduler.evaluate(daemon.policy, view,
                                          {"pinned_model": "luna", "pinned_lane": pin, "exclusions": "[]"})
            seen[key] = decision.chosen_lane, decision.evaluations[0]["rejections"][0]["reasons"]
        record = daemon._probe_record(holder)
        assert record["timer_kind"] == "touch" and record["model_id"] == "gpt-5.6-luna"
        (Path(record["directory"]) / "request.json").write_text(json.dumps({"requested_at": iso(datetime.now(timezone.utc))}))
        daemon.wham.started[lane.lane_id] = datetime.now(timezone.utc)
        return Outcome(OutcomeClass.OK, "OK", evidence={"rc": 0})

    monkeypatch.setattr(daemon, "_execute_probe", execute)
    result = daemon.timers.touch(target=touched.lane_id, mode="operator", request_id="req-1")
    assert [row["status"] for row in result["results"]] == ["ok"]
    assert seen["model"] == "gpt-5.6-luna" and seen["kind"] == "probe" and seen["sandbox"] == "read-only"
    assert seen["lease"] == seen["holder"] and seen["holder"].startswith("probe:timer:")
    assert seen["reserved"] == 1 and seen["unavailable"] == {touched.lane_id: seen["holder"]}
    assert touched.lane_id not in seen["open"]
    assert seen["pinned"] == (None, ["no-slot"])
    assert seen["fleet"][0] is None and "no-slot" in seen["fleet"][1]      # the fleet's one slot is the touch's
    assert daemon.store.list_leases() == [] and daemon.store.list_jobs() == []
    requests = [json.loads(row["data_json"]) for row in daemon.store.query(
        "SELECT data_json FROM events WHERE kind='timer.request' AND lane_id=?", (touched.lane_id,))]
    assert [row["purpose"] for row in requests if row] == ["touch"]
    view = daemon._capacity_view()
    row = next(row for row in view["lanes"] if row["lane_id"] == touched.lane_id)
    assert row["weekly_clock"] is None and row["clock_touch"]["status"] == "ok"
    assert next(row for row in view["lanes"] if row["lane_id"] == other.lane_id)["weekly_clock"] == "not-started"


def test_a_touch_recovered_after_a_restart_records_how_it_ended(daemon):
    """C-18.3, C-5.3: a daemon that died mid-touch finishes the record, and the spacing holds."""
    lane = codex(daemon, 1)
    started = {"lane_id": lane.lane_id, "at": iso(datetime.now(timezone.utc)), "mode": "auto",
               "model": "gpt-5.6-luna", "status": "touching"}
    daemon.timers._record_touch(lane.lane_id, started)
    directory = daemon.root / "lanes" / lane.lane_id / "probes" / "token"
    directory.mkdir(parents=True)
    sent = iso(datetime.now(timezone.utc))
    (directory / "request.json").write_text(json.dumps({"requested_at": sent}))
    daemon.store.acquire_lease(f"lane:{lane.lane_id}:slot:0", "probe:timer:token")
    record = {"holder": "probe:timer:token", "job_id": None, "lane_id": lane.lane_id, "timer_kind": "touch",
              "model_id": "gpt-5.6-luna", "directory": str(directory), "state": "running"}
    daemon._finish_probe(record, Outcome(OutcomeClass.OK, "OK", evidence={"rc": 0}))
    touch = daemon.timers.touches[lane.lane_id]
    assert touch["status"] == "ok" and touch["recovered"] is True and touch["requested_at"] == sent
    assert touch["at"] == started["at"]
    assert daemon.store.list_leases() == []


@pytest.mark.parametrize("changes,words,fix", [
    ({"owner": LaneOwner.V1}, "is owned by v1", "subfleet lanes transfer codex-1 --to v2"),
    ({"enabled": False}, "is disabled", "subfleet login codex codex-1"),
])
def test_a_named_lane_no_touch_may_use_is_refused_with_the_fix(daemon, changes, words, fix):
    """C-18.3, C-6.5, C-17.3: `lanes touch <lane>` refuses (exit 7) a lane no touch may use."""
    codex(daemon, 1, **changes)
    with pytest.raises(protocol.ProtocolError) as refused:
        daemon.dispatch("lanes", {"action": "touch", "lane_id": "codex-1"})
    assert refused.value.code == Exit.REFUSED and words in str(refused.value)
    assert fix in refused.value.fix


def test_limited_and_busy_named_lanes_are_refused(daemon):
    """C-18.3: a limited window has started; a live attempt is the first request itself."""
    limited, busy = codex(daemon, 1), codex(daemon, 2)
    daemon.timers._persist(limited, {"status": "limited", "limit_reached": True, "allowed": False,
                                     "account_key": limited.account_key, "readings": ()})
    daemon.store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                         state="running", workdir=str(daemon.root), prompt_path="/prompt", sandbox="read-only")
    daemon.store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id=busy.lane_id,
                             model_requested="gpt-6-astra", state="running", started_at=iso(datetime.now(timezone.utc)))
    for lane, words in ((limited, "usage limit"), (busy, "running an attempt")):
        with pytest.raises(protocol.ProtocolError) as refused:
            daemon.dispatch("lanes", {"action": "touch", "lane_id": lane.lane_id})
        assert refused.value.code == Exit.REFUSED and words in str(refused.value)


def test_dry_run_and_a_quiet_fleet_write_nothing_and_unknown_lanes_are_invalid(daemon):
    """C-18.3, C-17.3: a dry run plans without a turn, event, or lease; a name must be a Codex lane."""
    lane = codex(daemon, 1)
    measure(daemon, lane)
    before = daemon.store.one("SELECT count(*) n FROM events")["n"]
    touch = daemon.dispatch("lanes", {"action": "touch", "dry_run": True})["touch"]
    assert touch["status"] == "dry-run" and touch["touching"] == ["codex-1"] and touch["model"] == "gpt-5.6-luna"
    [entry] = touch["plan"]
    assert (entry["action"], entry["reason"], entry["weekly_clock"]) == ("touch", "not-started", "not-started")
    assert daemon.store.one("SELECT count(*) n FROM events")["n"] == before and daemon.store.list_leases() == []
    daemon.wham.started[lane.lane_id] = datetime.now(timezone.utc) - timedelta(days=1)
    measure(daemon, lane)
    quiet = daemon.dispatch("lanes", {"action": "touch"})["touch"]
    assert quiet["status"] == "nothing-to-touch" and quiet["plan"][0]["reason"] == "started"
    with pytest.raises(protocol.ProtocolError) as unknown:
        daemon.dispatch("lanes", {"action": "touch", "lane_id": "codex-9"})
    assert unknown.value.code == Exit.INVALID_INPUT
    with pytest.raises(protocol.ProtocolError):
        daemon.dispatch("lanes", {"action": "touch-status"})


def test_a_name_a_claude_and_a_codex_lane_share_names_the_codex_one(daemon):
    """C-11.2, C-18.3: an email that is both a Claude and a Codex lane names the Codex lane here."""
    home = daemon.root / "codex-7"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"tokens": {"account_id": "7", "access_token": "FAKE-ONLY"}}))
    daemon.store.put_lane(Lane("codex-7", "codex", "codex:max@example.org", Credential("codex", str(home), "home"),
                               str(home), LaneOwner.V2, False))
    daemon.store.put_lane(Lane("claude-7", "claude", "claude:a:o", Credential("claude", "TOKEN_VAR", "env"), None,
                               LaneOwner.V2, False, label="max@example.org"))
    touch = daemon.dispatch("lanes", {"action": "touch", "lane_id": "max@example.org", "dry_run": True})["touch"]
    assert touch["target"] == "codex-7" and [row["lane_id"] for row in touch["plan"]] == ["codex-7"]


def test_a_guard_preflight_refusal_stops_the_touch_before_any_launch(daemon, monkeypatch):
    """C-18.3, C-14.1, C-14.2: the touch runs the real guard preflight; with no reviewed overlay
    in the state root it refuses (code 7) before any provider process, and leaves no probe
    directory behind."""
    from subfleet.adapters.codex import CodexAdapter
    fake = Path(__file__).resolve().parents[1] / "bin" / "codex"
    monkeypatch.setattr("subfleet.daemon.get_adapter", lambda provider: CodexAdapter(codex_bin=str(fake)))
    lane = codex(daemon, 1)
    measure(daemon, lane)
    result = daemon.timers.touch(target=lane.lane_id, mode="operator", request_id="req-g")
    [record] = result["results"]
    assert record["status"] == "refused" and record["code"] == 7
    assert "guard preflight refused" in record["detail"].lower()
    states = [json.loads(row["data_json"]) for row in daemon.store.query(
        "SELECT data_json FROM events WHERE kind='probe.state' AND lane_id=? ORDER BY event_id", (lane.lane_id,))]
    states = [state for state in states if state]
    assert states and not any(state.get("guardian_pid") for state in states)     # no guardian, no provider
    assert states[-1]["state"] == "completed" and states[-1]["error_type"] == "AdapterError"
    assert daemon.store.list_leases() == []
    assert list((daemon.root / "lanes" / lane.lane_id / "probes").iterdir()) == []
    log = (daemon.root / "daemon.log").read_text()
    assert "guard preflight" in log and f"lane touch" in log and "status=refused" in log


def test_a_recovered_touch_that_found_the_credential_dead_disables_the_lane(daemon):
    """C-18.3, C-23.44: recovery acts on a touch's verdict as the live path does."""
    lane = codex(daemon, 1)
    daemon.timers._record_touch(lane.lane_id, {"lane_id": lane.lane_id, "at": iso(datetime.now(timezone.utc)),
                                               "mode": "auto", "status": "touching"})
    directory = daemon.root / "lanes" / lane.lane_id / "probes" / "token"
    directory.mkdir(parents=True)
    record = {"holder": "probe:timer:token", "job_id": None, "lane_id": lane.lane_id, "timer_kind": "touch",
              "model_id": "gpt-5.6-luna", "directory": str(directory), "state": "running"}
    daemon._finish_probe(record, Outcome(OutcomeClass.AUTH_DEAD, "refresh token was revoked", evidence={"rc": 1}))
    assert not daemon.store.get_lane(lane.lane_id).enabled
    assert daemon.timers.touches[lane.lane_id]["status"] == "auth-dead"


def test_naming_a_superseded_lane_touches_the_lane_that_replaced_it(daemon):
    """C-18.3, C-11.2: a re-enrolled lane's old id resolves to its successor on the same credential."""
    old = codex(daemon, 1, enabled=False)
    daemon.store.put_lane(Lane("codex-2", "codex", "codex:1", Credential("codex", old.home, "home"), old.home,
                               LaneOwner.V2, False))
    touch = daemon.dispatch("lanes", {"action": "touch", "lane_id": "codex-1", "dry_run": True})["touch"]
    assert touch["target"] == "codex-2" and [row["action"] for row in touch["plan"]] == ["touch"]
