"""Menu bar evidence honesty: C-5.7a, C-8.1, C-9.1, C-9.7, C-18.1, C-23.18."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet.capacity import PROBE_LEASES, build_view, mark_probe_leases
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Reading, ReadingLabel
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


# --- C-18.2, C-26.12, C-29.6: turn jobs belong to their conversations (IR-18) ----

def _turn(job_id, state, conversation, **extra):
    return _job(job_id, state, kind="turn", name=f"turn-{conversation}", pinned_model="opus", **extra)


def test_c18_2_c29_6_turn_jobs_leave_live_recent_and_counts_and_every_row_carries_kind():
    """C-18.2, C-26.12, C-29.6 (IR-18): a turn job is its conversation's, never detached work."""
    from subfleet.status_json import displayed_job_ids
    jobs = [_job("j-running-1", "running", kind="dispatch"),
            _turn("t-running-2", "running", "cv-a"),
            _turn("t-waiting-3", "waiting", "cv-b", wait_reason="capacity"),
            _job("j-done-4", "succeeded", kind="resume", rc=0, finished_at="2026-09-20T15:10:00Z"),
            _turn("t-done-5", "succeeded", "cv-a", rc=0, finished_at="2026-09-20T15:20:00Z")]
    snapshot = {"lanes": [], "jobs": jobs}
    result = build_status(snapshot)["jobs"]
    assert [row["job_id"] for row in result["live"]] == ["j-running-1"]
    assert [row["job_id"] for row in result["recent"]] == ["j-done-4"]
    assert result["counts"] == {"queued": 0, "running": 1, "waiting": 0}
    assert [row["kind"] for row in result["live"] + result["recent"]] == ["dispatch", "resume"]
    assert displayed_job_ids(snapshot) == ["j-running-1", "j-done-4"]          # no batch lookups for turns


def _summary(*items, **counts):
    return {"available": True, "counts": {"active": 2, "needs_approval": 1, "blocked": 0, **counts},
            "items": list(items), "truncated": False}


def _item(cid, state="running", pending=0):
    return {"conversation_id": cid, "provider": "claude", "title": f"title {cid}", "state": state,
            "blocked_by": None, "pending_approvals": pending, "updated_at": "2026-09-20T15:00:00Z"}


def test_c29_6_conversations_section_groups_live_turns_by_conversation():
    """C-29.6, D-26 (IR-18): each listed conversation carries its live turn; turns are counted apart."""
    jobs = [_turn("t-running-2", "running", "cv-a", started_at="2026-09-20T15:02:00Z"),
            _turn("t-old-1", "succeeded", "cv-a", rc=0, finished_at="2026-09-20T15:01:00Z"),
            _turn("t-waiting-3", "waiting", "cv-b", wait_reason="capacity", next_check_at="2026-09-20T15:09:00Z"),
            _job("j-running-9", "running", kind="dispatch")]
    attempts = [{"job_id": "t-running-2", "seq": 1, "lane_id": "claude-4", "model_requested": "claude-opus-5-5"}]
    snapshot = {"lanes": [], "jobs": jobs, "attempts": attempts,
                "conversations": _summary(_item("cv-a", "approval-needed", pending=1), _item("cv-b", "waiting"),
                                          _item("cv-c", "queued"))}
    section = build_status(snapshot)["conversations"]
    assert section["available"] is True and section["error"] is None
    assert section["counts"] == {"active": 2, "needs_approval": 1, "blocked": 0}
    assert section["turns"] == {"queued": 0, "running": 1, "waiting": 1}
    a, b, c = section["items"]
    assert (a["conversation_id"], a["state"], a["pending_approvals"]) == ("cv-a", "approval-needed", 1)
    assert a["turn"]["job_id"] == "t-running-2" and a["turn"]["kind"] == "turn"
    assert (a["turn"]["lane_id"], a["turn"]["model"]) == ("claude-4", "claude-opus-5-5")
    assert (b["turn"]["state"], b["turn"]["wait_reason"]) == ("waiting", "capacity")
    assert c["turn"] is None                                                  # queued, no turn job yet
    assert json.loads(json.dumps(section)) == section


@pytest.mark.parametrize("summary, error", [(None, "not-read"), ({"available": False, "error": "OperationalError"},
                                                                  "OperationalError")])
def test_c29_6_an_unread_conversation_store_is_unknown_never_zero(summary, error):
    """C-29.6, C-9.1's spirit: counts nobody observed are absent, not 0; turn counts still come from jobs."""
    snapshot = {"lanes": [], "jobs": [_turn("t-running-2", "running", "cv-a")]}
    if summary is not None:
        snapshot["conversations"] = summary
    section = build_status(snapshot)["conversations"]
    assert section == {"available": False, "error": error, "counts": None, "items": [], "truncated": False,
                       "turns": {"queued": 0, "running": 1, "waiting": 0}}


# --- C-29.6, D-27: per-account windows keyed by (scope, window) (IR-34) -------------

FABLE = "claude-fable-5-1"
NAMES = {FABLE: "fable", "claude-opus-5-5": "opus"}


def claude_reading(scope="account", window="seven_day", utilization=0.4, **extra):
    return reading("claude", scope=scope, window=window, utilization=utilization, **extra)


def test_c29_6_every_provider_window_is_published_keyed_by_scope_and_window():
    """C-29.6, C-9.8, C-9.9, D-27 (IR-34): five-hour, weekly and each model-scoped weekly window,
    each with percent, reset and evidence label; admission evidence is not a window."""
    rows = [claude_reading(window="five_hour", utilization=0.1, resets_at="2026-09-05T15:00:00Z", source="oauth-usage"),
            claude_reading(utilization=0.4, resets_at="2026-09-09T12:00:00Z", source="oauth-usage"),
            claude_reading(scope=FABLE, utilization=0.9, resets_at="2026-09-08T12:00:00Z", source="oauth-usage"),
            claude_reading(scope="claude-opus-5-5", utilization=0.5, observed_at="2026-09-05T11:00:00Z"),
            claude_reading(scope=FABLE, window="admission", utilization=None, label="admission-observed",
                           resets_at="2026-09-05T13:00:00Z", source="rate_limit_event")]
    view = build_view([lane("claude")], rows, now=NOW)
    windows = build_status({**view, "model_names": NAMES})["claude"]["accounts"][0]["windows"]
    assert [(w["scope"], w["window"], w["model"]) for w in windows] == [
        ("account", "five_hour", None), ("account", "seven_day", None),
        (FABLE, "seven_day", "fable"), ("claude-opus-5-5", "seven_day", "opus")]
    five, week, fable, opus = windows
    assert (five["used_percent"], five["reset_at"], five["status"], five["stale"]) == (
        10, "2026-09-05T15:00:00Z", "provider", False)
    assert (week["used_percent"], week["reset_at"]) == (40, "2026-09-09T12:00:00Z")
    assert (fable["used_percent"], fable["reset_at"], fable["status"], fable["source"]) == (
        90, "2026-09-08T12:00:00Z", "provider", "oauth-usage")
    assert (opus["status"], opus["stale"], opus["used_percent"]) == ("stale-provider", True, 50)
    assert all(key in fable for key in ("as_of", "age_s"))


def test_c29_6_a_model_scoped_window_never_replaces_the_account_window():
    """C-29.6, C-9.1 (IR-34): the Fable weekly reading, however new or full, is never the account's weekly."""
    rows = [claude_reading(utilization=0.3, observed_at="2026-09-05T11:59:00Z"),
            claude_reading(scope=FABLE, utilization=1.0, observed_at=NOW)]
    account = build_status(build_view([lane("claude")], rows, now=NOW))["claude"]["accounts"][0]
    assert account["probe"]["seven_day"]["used_percent"] == 30
    assert account["live"]["seven_day_pct"] == 30
    assert {(w["scope"], w["used_percent"]) for w in account["windows"]} == {("account", 30), (FABLE, 100)}
    only_scoped = build_status(build_view([lane("claude")], [claude_reading(scope=FABLE)], now=NOW))
    account = only_scoped["claude"]["accounts"][0]
    assert "seven_day" not in account["probe"] and "seven_day_pct" not in account["live"]
    assert [(w["scope"], w["model"]) for w in account["windows"]] == [(FABLE, None)]   # no policy names given


@pytest.mark.parametrize("label", ["admission-observed", "local-backoff", "unknown"])
def test_c29_6_windows_come_from_provider_labels_only(label):
    """C-9.1, C-29.6 (IR-34): no other label yields a window row, whatever number it carries."""
    rows = [claude_reading(label=label), claude_reading(scope=FABLE, label=label)]
    account = build_status(build_view([lane("claude")], rows, now=NOW))["claude"]["accounts"][0]
    assert account["windows"] == []


def test_c29_6_one_row_per_scope_and_window_the_newer_reading_wins():
    """C-29.6 (IR-34): rows are keyed by (scope, window) even from readings no view deduplicated."""
    from subfleet.status_json import scoped_windows
    raw = {"readings": [claude_reading(scope=FABLE, utilization=0.2, observed_at="2026-09-05T11:00:00Z", reading_id=1),
                        claude_reading(scope=FABLE, utilization=0.7, observed_at=NOW, reading_id=2),
                        claude_reading(utilization=0.5, observed_at="2026-09-05T10:00:00Z"),
                        claude_reading(utilization=float("nan")), claude_reading(utilization=1.5)]}
    rows = scoped_windows(raw, NAMES)
    assert [(w["scope"], w["window"], w["used_percent"]) for w in rows] == [
        ("account", "seven_day", 50), (FABLE, "seven_day", 70)]


def test_c29_6_claude_earliest_reset_is_the_soonest_future_account_reset_admission_could_use():
    """D-27, C-29.6 (IR-34): desktop, disabled and v1 lanes, past resets and model scopes do not count."""
    def claude_lane(lane_id, **extra):
        return lane("claude", lane_id=lane_id, account_key=f"claude:{lane_id}@example.org", **extra)

    def at(lane_id, window, resets_at, scope="account"):
        return claude_reading(lane_id=lane_id, window=window, scope=scope, resets_at=resets_at)
    lanes = [claude_lane("claude-1"), claude_lane("claude-2"), claude_lane("claude-desk", desktop=True),
             claude_lane("claude-off", enabled=False), claude_lane("claude-v1", owner="v1")]
    rows = [at("claude-1", "seven_day", "2026-09-09T00:00:00Z"), at("claude-1", "five_hour", "2026-09-05T11:00:00Z"),
            at("claude-2", "five_hour", "2026-09-05T16:00:00Z"),
            at("claude-2", "seven_day", "2026-09-05T12:30:00Z", scope=FABLE),
            *(at(name, "five_hour", "2026-09-05T12:10:00Z") for name in ("claude-desk", "claude-off", "claude-v1"))]
    result = build_status(build_view(lanes, rows, now=NOW, desktop_account="claude-desk@example.org"))
    assert result["claude"]["earliest_reset"] == "2026-09-05T16:00:00Z"
    assert build_status(build_view([claude_lane("claude-1")], now=NOW))["claude"]["earliest_reset"] is None


def test_c29_6_c10_6_an_identity_mismatched_lane_is_not_where_capacity_returns():
    """C-29.6, C-10.6: a lane whose credential proved to hold another account stays enabled and keeps its
    last bound readings (`stale-provider`), but admission never uses it, so its reset is not
    `earliest_reset` even when it is the soonest. Its windows still show."""
    def claude_lane(lane_id, **extra):
        return lane("claude", lane_id=lane_id, account_key=f"claude:{lane_id}@example.org", **extra)
    lanes = [claude_lane("claude-ok"), claude_lane("claude-mm", identity_status="mismatch")]
    rows = [claude_reading(lane_id="claude-ok", resets_at="2026-09-09T00:00:00Z"),
            claude_reading(lane_id="claude-mm", resets_at="2026-09-06T00:00:00Z", observed_at="2026-09-05T09:00:00Z")]
    view = build_view(lanes, rows, now=NOW)
    result = build_status(view)
    assert result["claude"]["earliest_reset"] == "2026-09-09T00:00:00Z"
    mismatched = next(row for row in result["claude"]["accounts"] if row["lane_id"] == "claude-mm")
    assert mismatched["enrolled"] and mismatched["identity_status"] == "mismatch"
    assert [(w["status"], w["reset_at"]) for w in mismatched["windows"]] == [("stale-provider", "2026-09-06T00:00:00Z")]
    # The same lane with a verified identity would be the answer: only the mismatch leaves it out.
    lanes[1] = claude_lane("claude-mm", identity_status="verified")
    assert build_status(build_view(lanes, rows, now=NOW))["claude"]["earliest_reset"] == "2026-09-06T00:00:00Z"


@pytest.mark.parametrize("resets_at", [None, "", "not a time", 1790000000])
def test_c29_6_an_unreadable_reset_clock_is_null_not_an_error(resets_at):
    """C-29.6 a window whose reset clock cannot be read still publishes, with `reset_at` null."""
    from subfleet.status_json import scoped_windows
    rows = scoped_windows({"readings": [claude_reading(resets_at=resets_at)]})
    assert [(row["used_percent"], row["reset_at"]) for row in rows] == [(40, None)]


# --- C-18.1: a lane a probe holds ------------------------------------------------

#: What `build_status` wrote before the probe fields (and what app/SubfleetApp.swift
#: decodes from it), so an addition can be told from a change.
V1_CODEX_ROW = {"lane_id", "verdict", "enabled", "owner", "dispatchable", "home", "email", "windows",
                "duplicate_of", "account_key", "app_shadowed", "reset_credits_remaining"}
V1_CLAUDE_ROW = {"lane_id", "verdict", "enabled", "owner", "dispatchable", "email", "active", "enrolled",
                 "probe", "live", "oauth_status", "desktop_in_use", "windows"}
V1_FLEET = {"total_homes", "dispatchable_now", "best_home", "earliest_reset", "reset_credits_remaining"}
V1_CLAUDE_LANES = {"enrolled", "dispatchable_now"}
PROBE_ROW = {"probe_state", "probe_holder"}
PROBE_STATES = ("reserved", "starting", "containing", "quarantined", "completed", "uncertain")


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
    assert set(result) == {"generated_at", "offline", "jobs", "conversations", "alerts", "codex", "claude"}
    assert result["alerts"] == []                       # C-18.4: always present, empty with none in force
    assert set(result["claude"]) == {"accounts", "earliest_reset", "lanes", "cards"}   # C-9.10
    assert result["claude"]["cards"] == {"read_at": None, "disabled": False, "accounts": [], "warnings": []}
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
    assert fenced["claude"]["earliest_reset"] == bare["claude"]["earliest_reset"]


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
                       deliver=lambda notice: sent.append(notice["key"]))
        timer.probe_record = records.get

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


ABSENT = object()


def _cap(values):
    return st.one_of(st.just(ABSENT), st.none(), values)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(cases=st.lists(LANE_CASE, min_size=3, max_size=3), fleet_cap=_cap(st.integers(1, 8)),
       per_lane=_cap(st.integers(1, 3)), unmeasured=_cap(st.integers(1, 2)))
def test_c18_1_status_json_and_admission_judge_probe_leases_alike(fleet, cases, fleet_cap, per_lane, unmeasured):
    """C-6.4, C-18.1 differential: `fence_probes` after `enrich_view` (status.json) agrees with the daemon's
    order, `mark_probe_leases` before `enrich_view` (admission), on every lane's verdict and probe fields, whether
    a cap is set, null or absent (no cap, #72); and in both a lane a probe lease names is out, whatever else holds."""
    timer, store, records, sent, enroll = fleet
    if not store.list_lanes():
        enroll(1, 2, 3)
    caps = {"max_active_attempts": fleet_cap, "max_in_flight_per_lane": per_lane,
            "max_in_flight_unmeasured": unmeasured}
    timer.policy["caps"] = {key: value for key, value in caps.items() if value is not ABSENT}
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
    held = {f"codex-{n}" for n, case in enumerate(cases, 1) if case["probe"]}
    for judged in (status, admission):
        assert held <= set(judged["unavailable_lanes"])
        for row in judged["lanes"]:
            assert (row.get("probe_holder") is not None) == (row["lane_id"] in held)
            if row["lane_id"] in held:
                assert not row["dispatchable"]
                assert row["probe_state"] == {"no-record": "uncertain"}.get(cases[int(row["lane_id"][6:]) - 1]["probe"],
                                                                         cases[int(row["lane_id"][6:]) - 1]["probe"])


def test_c18_1_the_published_verdict_is_one_committed_states(fleet):
    """C-18.1 the leases are read with the snapshot's rows: a probe that ends and closes its lane in one commit
    between the snapshot and the publication never publishes the lane as dispatchable (it was held, then closed)."""
    timer, store, records, sent, enroll = fleet
    enroll(1, 2)
    store.acquire_lease("lane:codex-1:slot:0", "probe:finishing")
    records["probe:finishing"] = {"holder": "probe:finishing", "state": "running"}
    snapshot = timer.snapshot()
    with store.transaction("test.probe_finished"):
        store.release_leases("probe:finishing")
        store.add_closure(Closure("codex-1", "account", iso(AT + timedelta(hours=1)),
                                  ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "wham"))
    records["probe:finishing"] = {"holder": "probe:finishing", "state": "completed"}
    timer.publish_status(snapshot)
    status, homes = _published(timer)
    assert not homes["codex-1"]["dispatchable"]
    assert (homes["codex-1"]["probe_state"], homes["codex-1"]["probe_holder"]) == ("running", "probe:finishing")
    assert status["codex"]["fleet"]["dispatchable_now"] == 1
    timer.publish_status(timer.snapshot())
    status, homes = _published(timer)
    assert not homes["codex-1"]["dispatchable"] and homes["codex-1"]["probe_state"] is None


def _fresh_weekly(store, lane_id, utilization=.2):
    store.add_reading(Reading(lane_id, "account", "seven_day", utilization, iso(AT + timedelta(days=1)),
                              ReadingLabel.PROVIDER, "wham", iso(AT - timedelta(seconds=5))))


def test_c18_1_c19_the_probe_cycles_reset_credit_policy_judges_before_the_leases(fleet):
    """C-18.1, C-19 an admission probe's seconds on the only lane with headroom do not send the policy looking
    for a credit to spend: it judges the snapshot before the leases, and status.json shows the lane held."""
    timer, store, records, sent, enroll = fleet
    enroll(1)
    timer.policy["reset_credits"] = {"enabled": True}
    _fresh_weekly(store, "codex-1")
    store.acquire_lease("lane:codex-1:slot:0", "probe:admission")
    records["probe:admission"] = {"holder": "probe:admission", "state": "reserved"}
    snapshot = timer.probe_cycle()
    status, homes = _published(timer)
    assert homes["codex-1"]["probe_state"] == "reserved" and not homes["codex-1"]["dispatchable"]
    assert snapshot["reset_policy"]["status"] == "not-triggered", snapshot["reset_policy"]


def test_c18_1_c19_a_reset_credit_pass_judges_before_the_leases(fleet):
    """C-18.1, C-19 the reset-credit timer's own pass judges the snapshot before the leases, as the cycle's does."""
    timer, store, records, sent, enroll = fleet
    enroll(1)
    timer.policy["reset_credits"] = {"enabled": True}
    _fresh_weekly(store, "codex-1")
    store.acquire_lease("lane:codex-1:slot:0", "probe:admission")
    assert timer.reset_credits_cycle()["status"] == "not-triggered"
    status, homes = _published(timer)
    assert homes["codex-1"]["probe_state"] == "uncertain" and not homes["codex-1"]["dispatchable"]


def test_c18_1_mark_probe_leases_takes_out_every_lane_a_lease_names():
    """C-11.4, C-18.1 every lane lease a probe holds takes its lane out and names it; a lease on no lane counts
    toward the fleet and marks nothing; another reason for a lane already out is kept, laid first or last."""
    view = {"lanes": [{"lane_id": "codex-1"}, {"lane_id": "codex-2"}, {"lane_id": "codex-3"}]}
    leases = [{"lease_key": "lane:codex-1:slot:0", "holder": "probe:a"},
              {"lease_key": "lane:codex-9:slot:0", "holder": "probe:gone-lane"},
              {"lease_key": "out:/tmp/x", "holder": "probe:not-a-lane"},
              {"lease_key": "lane:codex-3:slot:0", "holder": "probe:timer:read"}]
    records = {"probe:a": {"state": "quarantined"}, "probe:timer:read": {}}
    mark_probe_leases(view, leases, records.get)
    assert view["unavailable_lanes"] == {"codex-1": "probe:a", "codex-9": "probe:gone-lane", "codex-3": "probe:timer:read"}
    assert view["reserved_probes"] == 4
    lanes = {row["lane_id"]: row for row in view["lanes"]}
    assert (lanes["codex-1"]["probe_state"], lanes["codex-1"]["probe_holder"]) == ("quarantined", "probe:a")
    assert lanes["codex-3"]["probe_state"] == "uncertain"
    assert "probe_state" not in lanes["codex-2"] and "probe_holder" not in lanes["codex-2"]
    assert mark_probe_leases({"lanes": [{"lane_id": "codex-1"}]}, leases[:1])["lanes"][0]["probe_state"] == "uncertain"
    latched = {"lanes": [{"lane_id": "codex-1"}], "unavailable_lanes": {"codex-1": "credential-latched"}}
    assert mark_probe_leases(latched, leases[:1])["unavailable_lanes"] == {"codex-1": "credential-latched"}
    assert latched["lanes"][0]["probe_holder"] == "probe:a"


def test_c18_1_the_daemon_hands_the_timer_its_probe_records(tmp_path, monkeypatch):
    """C-18.1 wired as the daemon runs it: status.json names each held lane's probe state from the daemon's own
    records, and agrees with the daemon's capacity view on every lane's verdict and probe fields."""
    from subfleet.daemon import Daemon
    from subfleet import procs
    monkeypatch.setattr(procs, "boot_id", lambda: "unit-test-boot")
    monkeypatch.setattr(procs, "proc_start", lambda pid: "unit-test-start")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    daemon = Daemon(tmp_path / "root")
    try:
        for n in (1, 2, 3):
            home = tmp_path / f"codex-{n}"
            home.mkdir()
            daemon.store.put_lane(Lane(f"codex-{n}", "codex", f"codex:{n}", Credential("codex", str(home), "home"),
                                       str(home), LaneOwner.V2, False, True))
        daemon.store.acquire_lease("lane:codex-1:slot:0", "probe:abc")
        daemon._save_probe({"holder": "probe:abc", "job_id": None, "lane_id": "codex-1", "state": "quarantined"})
        daemon.store.acquire_lease("lane:codex-2:slot:0", "probe:timer:xyz")
        daemon.timers.publish_status(daemon.timers.snapshot())
        status = json.loads((daemon.root / "status.json").read_text())
        published = {row["lane_id"]: (row["dispatchable"], row["probe_state"], row["probe_holder"])
                     for row in status["codex"]["homes"]}
        admission = {row["lane_id"]: (row["dispatchable"], row.get("probe_state"), row.get("probe_holder"))
                     for row in daemon._capacity_view(desktop_in_use=False)["lanes"]}
        assert published == admission
        assert published["codex-1"] == (False, "quarantined", "probe:abc")
        assert published["codex-2"] == (False, "uncertain", "probe:timer:xyz")
        assert published["codex-3"][1:] == (None, None)
    finally:
        daemon.close()


def test_c18_1_a_commit_inside_the_snapshot_after_its_rows_is_not_published(tmp_path, monkeypatch):
    """C-18.1, C-3.7: the probe leases are read inside the snapshot the rows come from. A snapshot is fixed at its
    first read, so a commit that lands while the block is still open (a probe handing its lane to an attempt) must
    not reach the publication. A lease read placed just below the block would find the probe's lease gone while
    the rows still lack the attempt, and publish an occupied lane as dispatchable (the review of 87f87aef measured
    about 70 of 300 publications under churn). This needs the daemon's own store: a store with no read connections
    holds the store lock for the whole snapshot, so no commit can land inside it and the single-threaded tests
    above cannot see the difference."""
    import threading
    from subfleet.daemon import Daemon, utcnow
    from subfleet import procs
    monkeypatch.setattr(procs, "boot_id", lambda: "unit-test-boot")
    monkeypatch.setattr(procs, "proc_start", lambda pid: "unit-test-start")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    daemon = Daemon(tmp_path / "root")
    try:
        for n in (1, 2, 3):
            home = tmp_path / f"codex-{n}"
            home.mkdir()
            daemon.store.put_lane(Lane(f"codex-{n}", "codex", f"codex:{n}", Credential("codex", str(home), "home"),
                                       str(home), LaneOwner.V2, False, True))
        store, timers = daemon.store, daemon.timers
        daemon.policy["caps"].update({"max_in_flight_unmeasured": 1})
        assert store._max_readers > 0, "the daemon's store reads on its own connections"
        store.acquire_lease("lane:codex-1:slot:0", "probe:handoff")

        def hand_the_lane_to_an_attempt():
            with store.transaction("test.handoff") as tx:
                tx.execute("DELETE FROM leases WHERE holder=?", ("probe:handoff",))
                tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,"
                           "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           ("handoff", "handoff", "x", "dispatch", "running", "/tmp", "/tmp/p", "read-only", utcnow()))
                tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                           ("lane:codex-1:slot:0", "handoff/a1", utcnow()))
                tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at) "
                           "VALUES(?,?,1,'codex-1','gpt-6-astra','running',?)", ("handoff/a1", "handoff", utcnow()))

        view_rows = timers.view_rows

        def view_rows_then_a_commit(*args, **kwargs):
            rows = view_rows(*args, **kwargs)
            worker = threading.Thread(target=hand_the_lane_to_an_attempt)   # another thread's commit, mid-snapshot
            worker.start()
            worker.join(10)
            assert not worker.is_alive()
            return rows

        monkeypatch.setattr(timers, "view_rows", view_rows_then_a_commit)
        snapshot = timers.snapshot()
        monkeypatch.setattr(timers, "view_rows", view_rows)
        timers.publish_status(snapshot)
        homes = {row["lane_id"]: row for row in json.loads((daemon.root / "status.json").read_text())["codex"]["homes"]}
        admission = {row["lane_id"]: bool(row["dispatchable"])
                     for row in daemon._capacity_view(desktop_in_use=False)["lanes"]}
        assert not admission["codex-1"]                      # occupied before the commit and after it
        assert not homes["codex-1"]["dispatchable"], homes["codex-1"]
    finally:
        daemon.close()


def test_c6_4_c18_1_with_no_fleet_cap_probe_leases_take_out_only_their_own_lanes(fleet):
    """C-6.4 (#72), C-18.1 under the shipped policy, which sets no max_active_attempts, five probe leases take out
    five lanes and nothing else: the sixth is published dispatchable, as admission would place work there."""
    timer, store, records, sent, enroll = fleet
    enroll(1, 2, 3, 4, 5, 6)
    assert "max_active_attempts" not in timer.policy["caps"]
    for n in (1, 2, 3, 4, 5):
        store.acquire_lease(f"lane:codex-{n}:slot:0", f"probe:{n}")
    timer.publish_status(timer.snapshot())
    status, homes = _published(timer)
    assert [lane_id for lane_id, row in sorted(homes.items()) if row["dispatchable"]] == ["codex-6"]
    fleet_row = status["codex"]["fleet"]
    assert (fleet_row["dispatchable_now"], fleet_row["probe_held"], fleet_row["best_home"]) == (
        1, 5, homes["codex-6"]["home"])
