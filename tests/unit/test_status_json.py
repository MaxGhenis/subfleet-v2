"""Menu bar evidence honesty: C-8.1, C-9.1, C-9.7, C-18.1, C-23.18."""

import copy
import json

import pytest

from subfleet.capacity import build_view
from subfleet.status_json import build_status, write_status

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
