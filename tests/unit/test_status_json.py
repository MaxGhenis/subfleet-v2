"""Menu bar evidence honesty: C-5.7a, C-8.1, C-9.1, C-9.7, C-18.1, C-23.18."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet.capacity import PROBE_LEASES, build_view, mark_probe_leases
from subfleet.contracts import Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.status_json import build_status, write_status
from subfleet.store import Store
from subfleet.timers import Timers, iso

NOW = "2026-09-05T12:00:00Z"


def lane(provider="codex", **extra):
    return {"lane_id": f"{provider}-1", "provider": provider, "account_key": f"{provider}:first@example.org",
            "credential_ref": f"/homes/{provider}-1", "home": f"/homes/{provider}-1", "owner": "v2",
            "enabled": True, "desktop": False, **extra}


def reading(provider="codex", **extra):
    return {"lane_id": f"{provider}-1", "scope": "account", "window": "seven_day", "utilization": 0.25,
            "resets_at": "2026-09-06T12:00:00Z", "label": "provider", "source": "usage", "observed_at": NOW, **extra}


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("label", ["admission-observed", "local-backoff", "unknown"])
def test_no_percentage_without_provider_reading(provider, label):
    """C-9.1, C-18.1: non-provider evidence renders words even if it carries a number."""
    view = build_view([lane(provider)], [reading(provider, label=label)], now=NOW)
    result = build_status(view)
    serialized = json.dumps(result)
    assert "used_percent" not in serialized
    assert "_pct" not in serialized
    assert label in serialized


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_stale_provider_percentages_marked_stale(provider):
    """C-9.1: stale provider readings retain percentages and explicitly carry stale labels."""
    view = build_view([lane(provider)], [reading(provider, observed_at="2026-09-05T11:00:00Z")], now=NOW)
    result = build_status(view)
    if provider == "codex":
        window = result["codex"]["homes"][0]["windows"]["secondary"]
        assert result["codex"]["homes"][0]["windows"]["source"] == "stale-provider"
    else:
        window = result["claude"]["accounts"][0]["probe"]["seven_day"]
        assert result["claude"]["accounts"][0]["live"]["stale"]
    assert window["used_percent"] == 25
    assert window["stale"]
    assert window["status"] == "stale-provider"


def test_v1_swift_shape_and_duration_aliases(tmp_path):
    """C-8.1, C-9.7, C-18.1: Swift-compatible sections publish duration-based aliases atomically."""
    rows = [reading(window="seven_day", utilization=0.8), reading(window="five_hour", utilization=0.1),
            reading("claude", window="five_hour", utilization=0.2)]
    view = build_view([lane(), lane("claude")], rows, now=NOW)
    before = copy.deepcopy(view)
    result = write_status(tmp_path, view)
    assert json.loads((tmp_path / "status.json").read_text()) == result
    assert result["generated_at"] == NOW
    windows = result["codex"]["homes"][0]["windows"]
    assert windows["primary"]["used_percent"] == 10
    assert windows["secondary"]["used_percent"] == 80
    assert result["codex"]["fleet"]["total_homes"] == 1
    assert result["codex"]["fleet"]["dispatchable_now"] == 1
    claude = result["claude"]["accounts"][0]
    assert claude["email"] == "first@example.org"
    assert claude["enrolled"] and not claude["active"]
    assert isinstance(claude["probe"]["five_hour"]["reset_at"], float)
    assert view == before
    assert not list(tmp_path.glob(".status.json-*"))


def test_identity_status_and_post_heal_verdict_preserved():
    """C-23.27, C-23.45: status sees the healed verdict and canonical identity metadata."""
    view = build_view([lane(verdict="ok", identity_status="verified", app_shadowed=True)], now=NOW)
    row = build_status(view)["codex"]["homes"][0]
    assert row["verdict"] == "ok"
    assert row["identity_status"] == "verified"
    assert row["app_shadowed"]
    assert "used_percent" not in json.dumps(row)


@pytest.mark.parametrize("counts, total", [([2, 3], 5), ([2, None], None), ([0, 0], 0)])
def test_fleet_credit_total_unknown_if_any_count_unreadable(counts, total):
    """C-23.18: a partial fleet credit count never masquerades as a complete total."""
    lanes = [lane(lane_id=f"codex-{i}", account_key=f"codex:{i}", reset_credits_remaining=count) for i, count in enumerate(counts)]
    result = build_status(build_view(lanes, now=NOW))
    assert result["codex"]["fleet"]["reset_credits_remaining"] == total


def test_probe_credit_counts_include_disabled_unknown_and_deduplicate_accounts():
    """C-23.18, C-23.45: adapter counts are complete only across all canonical accounts, including disabled lanes."""
    lanes = [lane(probe={"reset_credits": {"available": 2}}),
             lane(lane_id="duplicate", duplicate_of="/homes/codex-1", reset_credits={"available": 2})]
    assert build_status(build_view(lanes, now=NOW))["codex"]["fleet"]["reset_credits_remaining"] == 2
    lanes.append(lane(lane_id="disabled", account_key="codex:disabled", enabled=False))
    assert build_status(build_view(lanes, now=NOW))["codex"]["fleet"]["reset_credits_remaining"] is None


def test_empty_roster_and_model_scope_cannot_supply_account_percentage():
    """C-9.1: an absent account window remains unknown despite model-specific provider evidence."""
    assert build_status({"lanes": [], "now": NOW})["codex"]["fleet"]["total_homes"] == 0
    view = build_view([lane()], [reading(scope="gpt-6-astra")], now=NOW)
    assert "used_percent" not in json.dumps(build_status(view))


def test_auth_dead_uses_existing_menu_warning_alias_without_losing_outcome():
    """C-18.1, C-23.44: disabled auth-dead lanes retain their class and activate Swift's existing credential warning."""
    row = build_status(build_view([lane(enabled=False, verdict="auth-dead")], now=NOW))["codex"]["homes"][0]
    assert row["verdict"] == "auth-revoked"
    assert row["outcome"] == "auth-dead"
    assert not row["dispatchable"]


# --- C-18.2: the jobs the menu shows -------------------------------------------

def _job(job_id, state, **extra):
    return {"job_id": job_id, "state": state, "name": job_id.split("-", 2)[-1], "sandbox": "workspace-write",
            "workdir": f"/work/{job_id}", "worktree": None, "pinned_model": "fable", "created_at": f"2026-09-20T15:0{job_id[-1]}:00Z",
            "started_at": None, "finished_at": None, "rc": None, "wait_reason": None, "next_check_at": None, **extra}


def test_c18_2_jobs_section_orders_live_work_and_keeps_recent_results():
    """C-18.2 running, then waiting with its reason, then queued; then the newest finished jobs."""
    from subfleet.status_json import RECENT_JOBS
    jobs = [_job("j-queued-3", "queued"),
            _job("j-waiting-2", "waiting", wait_reason="workspace", next_check_at="2026-09-20T15:09:00Z"),
            _job("j-running-1", "running", worktree="/state/worktrees/j-running-1", started_at="2026-09-20T15:01:30Z"),
            *[_job(f"j-done-{n}", "succeeded", rc=0, finished_at=f"2026-09-20T14:{n:02d}:00Z") for n in range(12)],
            _job("j-failed-9", "failed", rc=7, finished_at="2026-09-20T14:59:00Z", wait_reason="capacity")]
    attempts = [{"job_id": "j-running-1", "seq": 1, "lane_id": "claude-3", "model_requested": "claude-opus-5"},
                {"job_id": "j-running-1", "seq": 2, "lane_id": "claude-13", "model_requested": "claude-fable-5-1"}]
    batch = {"id": "h-0920", "label": "codex handoff", "index": 1, "size": 5}
    result = build_status({"lanes": [], "jobs": jobs, "attempts": attempts, "batches": {"j-running-1": batch}})["jobs"]
    assert [row["job_id"] for row in result["live"]] == ["j-running-1", "j-waiting-2", "j-queued-3"]
    assert result["counts"] == {"queued": 1, "running": 1, "waiting": 1}
    running, waiting, queued = result["live"]
    assert (running["lane_id"], running["model"], running["attempts"]) == ("claude-13", "claude-fable-5-1", 2)
    assert running["workdir"] == "/state/worktrees/j-running-1" and running["batch"] == batch
    assert (waiting["wait_reason"], waiting["next_check_at"]) == ("workspace", "2026-09-20T15:09:00Z")
    assert (queued["model"], queued["lane_id"], queued["batch"]) == ("fable", None, None)
    assert len(result["recent"]) == RECENT_JOBS and result["recent"][0]["job_id"] == "j-failed-9"
    assert result["recent"][0]["rc"] == 7 and result["recent"][0]["wait_reason"] is None   # a reason belongs to a waiting job
    assert [row["job_id"] for row in result["recent"][1:3]] == ["j-done-11", "j-done-10"]


def test_c18_2_a_snapshot_without_jobs_still_has_the_section():
    """C-18.2 the menu decodes one shape whether or not anything is running."""
    assert build_status({"lanes": []})["jobs"] == {"live": [], "recent": [], "counts": {"queued": 0, "running": 0, "waiting": 0}}


def test_c18_2_batch_labels_are_read_for_displayed_jobs_only(tmp_path):
    """C-17.7, C-18.2 labels come from `job.submitted` events, and only for jobs the menu shows."""
    from subfleet.status_json import attach_batches
    from subfleet.store import Store
    batch = {"id": "h-0920", "label": "codex handoff", "index": 2, "size": 5}
    with Store(tmp_path / "state.sqlite3") as store:
        store.add_event("job.submitted", job_id="j-running-1", data={"batch": batch, "write_target": "/work"})
        store.add_event("job.submitted", job_id="j-queued-3", data={"write_target": "/work/other"})
        store.add_event("job.submitted", job_id="j-ancient-0", data={"batch": batch})
        snapshot = {"jobs": [_job("j-running-1", "running"), _job("j-queued-3", "queued")]}
        attach_batches(store, snapshot)
        assert snapshot["batches"] == {"j-running-1": batch}
        empty = {"jobs": []}
        attach_batches(store, empty)
        assert empty["batches"] == {}


# --- C-18.1: a lane a probe holds ------------------------------------------------

#: What `build_status` wrote before the probe fields (and what app/SubfleetApp.swift
#: decodes from it), so an addition can be told from a change.
V1_CODEX_ROW = {"lane_id", "verdict", "enabled", "owner", "dispatchable", "home", "email", "windows",
                "duplicate_of", "account_key", "app_shadowed", "reset_credits_remaining"}
V1_CLAUDE_ROW = {"lane_id", "verdict", "enabled", "owner", "dispatchable", "email", "active", "enrolled",
                 "probe", "live", "oauth_status"}
V1_FLEET = {"total_homes", "dispatchable_now", "best_home", "earliest_reset", "reset_credits_remaining"}
V1_CLAUDE_LANES = {"enrolled", "dispatchable_now"}
PROBE_ROW = {"probe_state", "probe_holder"}
PROBE_STATES = ("reserved", "starting", "containing", "quarantined", "uncertain")


def _codex(n, **extra):
    return lane(lane_id=f"codex-{n}", home=f"/homes/codex-{n}", credential_ref=f"/homes/codex-{n}",
                account_key=f"codex:{n}@example.org", **extra)


@pytest.mark.parametrize("state", PROBE_STATES)
def test_c18_1_a_lane_a_probe_holds_is_not_dispatchable(state):
    """C-5.7a, C-18.1 a probe's lease keeps its lane out of dispatchable_now and best_home, however fresh its readings."""
    lanes = [_codex(1, dispatchable=True, probe_state=state, probe_holder="probe:q1"),
             _codex(2, dispatchable=True),
             lane("claude", dispatchable=True, probe_state=state, probe_holder="probe:q2")]
    rows = [reading(lane_id="codex-1"), reading(lane_id="codex-2"), reading("claude")]
    result = build_status(build_view(lanes, rows, now=NOW))
    homes = {row["lane_id"]: row for row in result["codex"]["homes"]}
    held, free = homes["codex-1"], homes["codex-2"]
    assert not held["dispatchable"]
    assert (held["probe_state"], held["probe_holder"]) == (state, "probe:q1")
    assert held["verdict"] == "ok" and held["windows"]["seven_day"]["used_percent"] == 25   # the quota evidence still shows
    assert free["dispatchable"] and free["probe_state"] is None and free["probe_holder"] is None
    fleet = result["codex"]["fleet"]
    assert (fleet["dispatchable_now"], fleet["best_home"], fleet["probe_held"]) == (1, "/homes/codex-2", 1)
    claude = result["claude"]["accounts"][0]
    assert not claude["dispatchable"] and claude["probe_state"] == state
    assert result["claude"]["lanes"] == {"enrolled": 1, "dispatchable_now": 0, "probe_held": 1}


def test_c18_1_probe_fields_are_additions_only():
    """C-18.1 the fields the menu bar app decodes keep their names and values; the probe fields are the only new ones."""
    rows = [reading(window="seven_day", utilization=0.8), reading("claude", window="five_hour", utilization=0.2)]
    result = build_status(build_view([lane(), lane("claude")], rows, now=NOW))
    codex, claude = result["codex"]["homes"][0], result["claude"]["accounts"][0]
    assert set(result) == {"generated_at", "offline", "jobs", "codex", "claude"}
    assert set(codex) == V1_CODEX_ROW | PROBE_ROW
    assert set(claude) == V1_CLAUDE_ROW | PROBE_ROW
    assert set(result["codex"]["fleet"]) == V1_FLEET | {"probe_held"}
    assert set(result["claude"]["lanes"]) == V1_CLAUDE_LANES | {"probe_held"}
    assert codex["probe_state"] is None and codex["probe_holder"] is None and claude["probe_state"] is None
    assert result["codex"]["fleet"]["probe_held"] == result["claude"]["lanes"]["probe_held"] == 0
    assert codex["dispatchable"] and claude["dispatchable"]
    assert result["codex"]["fleet"]["best_home"] == "/homes/codex-1"


ROSTER = st.lists(st.fixed_dictionaries({
    "provider": st.sampled_from(["codex", "claude"]), "enabled": st.booleans(),
    "owner": st.sampled_from(["v2", "v1"]), "desktop": st.booleans(), "duplicate": st.booleans(),
    "dispatchable": st.one_of(st.none(), st.booleans()), "measured": st.booleans(),
    "probe_state": st.one_of(st.none(), st.sampled_from(PROBE_STATES))}), max_size=7)


def _roster(specs):
    lanes = []
    for n, spec in enumerate(specs):
        provider = spec["provider"]
        row = lane(provider, lane_id=f"{provider}-{n}", home=f"/homes/{n}", credential_ref=f"/homes/{n}",
                   account_key=f"{provider}:{n}@example.org", enabled=spec["enabled"], owner=spec["owner"],
                   desktop=spec["desktop"],
                   readings=[reading(provider, lane_id=f"{provider}-{n}")] if spec["measured"] else [])
        if spec["dispatchable"] is not None:
            row["dispatchable"] = spec["dispatchable"]
        if spec["duplicate"]:
            row["duplicate_of"] = "/homes/elsewhere"
        if spec["probe_state"] is not None:
            row.update(probe_state=spec["probe_state"], probe_holder=f"probe:{n}")
        lanes.append(row)
    return lanes


@settings(max_examples=300, deadline=None)
@given(ROSTER)
def test_c18_1_the_probe_fence_takes_out_exactly_the_held_lanes(specs):
    """C-18.1 for every roster: no held lane is dispatchable; the fence takes out the held lanes and nothing else;
    the fleet counts and best_home follow the rows; every other field is what it is with no probe at all."""
    lanes = _roster(specs)
    fenced = build_status({"lanes": lanes, "now": NOW})
    bare = build_status({"lanes": [{k: v for k, v in row.items() if k not in PROBE_ROW} for row in lanes], "now": NOW})
    for section, totals, key in (("codex", "fleet", "homes"), ("claude", "lanes", "accounts")):
        rows, before = fenced[section][key], bare[section][key]
        assert not any(row["dispatchable"] for row in rows if row["probe_state"] is not None)
        assert [row["dispatchable"] for row in rows] == [
            was["dispatchable"] and row["probe_state"] is None for row, was in zip(rows, before, strict=True)]
        assert fenced[section][totals]["dispatchable_now"] == sum(row["dispatchable"] for row in rows)
        assert fenced[section][totals]["probe_held"] == sum(row["probe_state"] is not None for row in rows)
        for row, was in zip(rows, before, strict=True):
            unchanged = set(row) - PROBE_ROW - {"dispatchable"}
            assert {k: row[k] for k in unchanged} == {k: was[k] for k in unchanged}
    available = [row["home"] for row in fenced["codex"]["homes"] if row["dispatchable"]]
    assert fenced["codex"]["fleet"]["best_home"] == (available[0] if available else None)
    for section, totals in (("codex", "fleet"), ("claude", "lanes")):
        extra = {"probe_held"}
        assert ({k: v for k, v in fenced[section][totals].items() if k not in extra | {"dispatchable_now", "best_home"}}
                == {k: v for k, v in bare[section][totals].items() if k not in extra | {"dispatchable_now", "best_home"}})
    assert fenced["jobs"] == bare["jobs"]


# --- C-18.1: the timer lays the leases admission honours ---------------------------

AT = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


class Usage:
    """A Codex usage read that always answers: fresh headroom on both account windows."""

    def probe_status(self, lane, env):
        return {"status": "ok", "limit_reached": False, "readings": tuple(
            Reading(lane.lane_id, "account", window, .2, iso(AT + timedelta(hours=5)),
                    ReadingLabel.PROVIDER, "wham", iso(AT)) for window in ("five_hour", "seven_day"))}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    policy = {"models": {"haiku": {"id": "claude-haiku-4-5-20251001"}},
              "timers": {"probe_interval_s": 300, "keepalive_interval_s": 18300},
              "reset_credits": {"enabled": False}, "alerts": {}, "caps": {}}
    records, sent = {}, []
    with Store(tmp_path / "state.sqlite3") as store:
        timer = Timers(store, tmp_path, policy, adapter_factory=lambda _: Usage(), now=lambda: AT,
                       deliver=lambda notice: sent.append(notice["key"]), probe_record=records.get)

        def enroll(*numbers):
            for n in numbers:
                home = tmp_path / f"codex-{n}"
                home.mkdir()
                (home / "auth.json").write_text(json.dumps({"last_refresh": "first"}))
                store.put_lane(Lane(f"codex-{n}", "codex", f"codex:{n}", Credential("codex", str(home), "home"),
                                    str(home), LaneOwner.V2, False, True))
        yield timer, store, records, sent, enroll
        timer.stop()


def _published(timer):
    status = json.loads((timer.root / "status.json").read_text())
    return status, {row["lane_id"]: row for row in status["codex"]["homes"]}


def test_c18_1_a_probe_cycle_publishes_a_quarantined_probes_lane_as_held(fleet):
    """C-5.7a, C-18.1 the published cycle keeps a quarantined probe's lane out of the fleet, names the probe,
    and leaves the snapshot its reset-credit policy and alerts judge as it was."""
    timer, store, records, sent, enroll = fleet
    enroll(1, 2, 3)
    store.acquire_lease("lane:codex-1:slot:0", "probe:q1")
    records["probe:q1"] = {"holder": "probe:q1", "lane_id": "codex-1", "state": "quarantined"}
    timer.probe_cycle()
    status, homes = _published(timer)
    assert not homes["codex-1"]["dispatchable"]
    assert (homes["codex-1"]["probe_state"], homes["codex-1"]["probe_holder"]) == ("quarantined", "probe:q1")
    assert homes["codex-2"]["dispatchable"] and homes["codex-3"]["dispatchable"]
    assert homes["codex-2"]["probe_state"] is None and homes["codex-3"]["probe_state"] is None
    fleet_row = status["codex"]["fleet"]
    assert (fleet_row["dispatchable_now"], fleet_row["probe_held"]) == (2, 1)
    assert fleet_row["best_home"] in {homes["codex-2"]["home"], homes["codex-3"]["home"]}
    assert all(row["dispatchable"] for row in timer.snapshot()["lanes"])
    assert "probe_state" not in json.dumps(timer.snapshot()["lanes"])


def test_c18_1_alerts_judge_the_cycle_before_the_probe_leases(fleet):
    """C-18.1 the slot a probe holds does not raise a fleet alert: alerts judge the snapshot before the leases."""
    timer, store, records, sent, enroll = fleet
    enroll(1, 2)
    store.acquire_lease("lane:codex-1:slot:0", "probe:admission")
    records["probe:admission"] = {"holder": "probe:admission", "lane_id": "codex-1", "state": "reserved"}
    timer.probe_cycle()
    status, homes = _published(timer)
    assert status["codex"]["fleet"]["dispatchable_now"] == 1 and homes["codex-1"]["probe_state"] == "reserved"
    assert "codex-fleet-low" not in sent and "codex-fleet-empty" not in sent


def test_c18_1_a_probe_without_a_record_is_uncertain_and_its_release_frees_the_lane(fleet):
    """C-18.1 a lease no record names is shown `uncertain`; once released the lane is dispatchable again."""
    timer, store, records, sent, enroll = fleet
    enroll(1)
    store.acquire_lease("lane:codex-1:slot:0", "probe:timer:gone")
    timer.publish_status(timer.snapshot())
    status, homes = _published(timer)
    assert homes["codex-1"]["probe_state"] == "uncertain" and not homes["codex-1"]["dispatchable"]
    assert status["codex"]["fleet"]["dispatchable_now"] == 0 and status["codex"]["fleet"]["best_home"] is None
    store.release_leases("probe:timer:gone")
    timer.publish_status(timer.snapshot())
    status, homes = _published(timer)
    assert homes["codex-1"]["probe_state"] is None and homes["codex-1"]["dispatchable"]
    assert status["codex"]["fleet"]["probe_held"] == 0


def test_c18_1_probe_leases_count_toward_the_fleet_cap(fleet):
    """C-6.4, C-18.1 every probe lease counts toward max_active_attempts in status.json as in admission."""
    timer, store, records, sent, enroll = fleet
    enroll(1, 2, 3)
    timer.policy["caps"]["max_active_attempts"] = 2
    store.acquire_lease("lane:codex-1:slot:0", "probe:a")
    store.acquire_lease("lane:codex-2:slot:0", "probe:b")
    timer.publish_status(timer.snapshot())
    status, homes = _published(timer)
    assert not any(row["dispatchable"] for row in homes.values())
    assert homes["codex-3"]["probe_state"] is None
    assert (status["codex"]["fleet"]["dispatchable_now"], status["codex"]["fleet"]["probe_held"]) == (0, 2)
    store.release_leases("probe:b")
    timer.publish_status(timer.snapshot())
    status, homes = _published(timer)
    assert [lane_id for lane_id, row in sorted(homes.items()) if row["dispatchable"]] == ["codex-2", "codex-3"]


def test_c18_1_a_reset_credit_pass_publishes_the_held_lane_too(fleet):
    """C-18.1, C-19 the reset-credit timer's own publication lays the probe leases as the probe cycle's does."""
    timer, store, records, sent, enroll = fleet
    enroll(1, 2)
    store.acquire_lease("lane:codex-2:slot:0", "probe:q2")
    records["probe:q2"] = {"holder": "probe:q2", "state": "quarantined"}
    assert timer.reset_credits_cycle()["status"] == "disabled"
    status, homes = _published(timer)
    assert homes["codex-2"]["probe_state"] == "quarantined" and not homes["codex-2"]["dispatchable"]
    assert status["codex"]["fleet"]["best_home"] == homes["codex-1"]["home"]


LANE_CASE = st.fixed_dictionaries({
    "enabled": st.booleans(), "reading": st.sampled_from([None, "fresh", "stale"]),
    "utilization": st.floats(0, 1), "closed": st.booleans(), "in_flight": st.integers(0, 3),
    "revoked": st.booleans(), "probe": st.sampled_from([None, "quarantined", "reserved", "no-record"])})


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(cases=st.lists(LANE_CASE, min_size=3, max_size=3), fleet_cap=st.integers(1, 8),
       per_lane=st.integers(1, 3), unmeasured=st.integers(1, 2))
def test_c18_1_status_json_and_admission_judge_probe_leases_alike(fleet, cases, fleet_cap, per_lane, unmeasured):
    """C-18.1 differential: `fence_probes` after `enrich_view` (status.json) agrees with the daemon's order,
    `mark_probe_leases` before `enrich_view` (admission), on every lane's verdict and probe fields."""
    timer, store, records, sent, enroll = fleet
    if not store.list_lanes():
        enroll(1, 2, 3)
    timer.policy["caps"] = {"max_active_attempts": fleet_cap, "max_in_flight_per_lane": per_lane,
                            "max_in_flight_unmeasured": unmeasured}
    timer.metadata = {f"codex-{n}": {"probe_status": "revoked"} for n, case in enumerate(cases, 1) if case["revoked"]}
    records.clear()
    with store.transaction("test.leases") as tx:
        tx.execute("DELETE FROM leases")
    for n, case in enumerate(cases, 1):
        if case["probe"]:
            store.acquire_lease(f"lane:codex-{n}:slot:0", f"probe:{n}")
            if case["probe"] != "no-record":
                records[f"probe:{n}"] = {"holder": f"probe:{n}", "state": case["probe"]}

    def view():
        lanes = [{**row, "enabled": case["enabled"]} for row, case in zip(store.lane_rows(), cases, strict=True)]
        readings = [{"lane_id": f"codex-{n}", "scope": "account", "window": "seven_day",
                     "utilization": case["utilization"], "resets_at": iso(AT + timedelta(days=1)),
                     "label": "provider", "source": "wham",
                     "observed_at": iso(AT - timedelta(seconds=10 if case["reading"] == "fresh" else 3600))}
                    for n, case in enumerate(cases, 1) if case["reading"]]
        closures = [{"lane_id": f"codex-{n}", "scope": "account", "until_at": iso(AT + timedelta(hours=1)),
                     "reason": "provider-limit"} for n, case in enumerate(cases, 1) if case["closed"]]
        attempts = [{"lane_id": f"codex-{n}", "state": "running"}
                    for n, case in enumerate(cases, 1) for _ in range(case["in_flight"])]
        return build_view(lanes, readings, closures, attempts, now=AT)

    status = timer.fence_probes(timer.enrich_view(view()))
    admission = timer.enrich_view(mark_probe_leases(view(), store.query(PROBE_LEASES), records.get))
    fields = ("dispatchable", "probe_state", "probe_holder")
    assert ({row["lane_id"]: tuple(row.get(key) for key in fields) for row in status["lanes"]}
            == {row["lane_id"]: tuple(row.get(key) for key in fields) for row in admission["lanes"]})
    assert status["unavailable_lanes"] == admission["unavailable_lanes"]
    assert status["reserved_probes"] == admission["reserved_probes"] == sum(bool(case["probe"]) for case in cases)
