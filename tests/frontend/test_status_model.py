"""The real Swift menu model consumes daemon JSON without launching a GUI."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from subfleet.status_json import build_status
from tests.frontend.swift import compile_probe


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or shutil.which("xcrun") is None,
                                reason="native Swift frontend validation requires macOS developer tools")


@pytest.fixture(scope="session")
def probe(tmp_path_factory):
    """Compile production model code with Foundation only; no AppKit entry point."""
    return compile_probe(tmp_path_factory.mktemp("subfleet-swift-model") / "probe",
                         ROOT / "tests/frontend/StatusModelProbe.swift", "SUBFLEET_MODEL_TEST")


def lane(provider, *, label="provider", **overrides):
    return {"lane_id": f"{provider}-1", "provider": provider, "owner": "v2", "enabled": True,
            "account_key": f"{provider}:fixture", "email": "fixture@example.invalid",
            "readings": [{"scope": "account", "window": window, "label": label,
                          "source": "fixture", "utilization": utilization,
                          "observed_at": NOW.isoformat(), "resets_at": (NOW + timedelta(days=1)).isoformat()}
                         for window, utilization in (("five_hour", .25), ("seven_day", .60))],
            **overrides}


def display(probe, tmp_path, rows, *, generated_at=NOW, offline=False, now=NOW):
    return project(probe, tmp_path, build_status({"lanes": rows, "offline": offline}, now=generated_at), now)


def project(probe, tmp_path, payload, now=NOW):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(payload))
    result = subprocess.run([str(probe), str(path), str(now.timestamp())], check=True,
                            capture_output=True, text=True, timeout=10)
    return json.loads(result.stdout)


def test_c18_1_frontend_reads_live_provider_windows(probe, tmp_path):
    """C-9.1, C-18.1 the Swift model decodes actual Codex and Claude daemon projections."""
    result = display(probe, tmp_path, [lane("codex"), lane("claude")])
    assert result["stale"] is False
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert row["percentage"] == 25 and row["weekly_percentage"] == 60
        assert row["five_hour_reset"] == (NOW + timedelta(days=1)).timestamp()
        assert row["weekly_reset"] == (NOW + timedelta(days=1)).timestamp()
        assert row["stale"] is False
        assert row["tone"] == "good"


@pytest.mark.parametrize("label", ["unknown", "admission-observed", "local-backoff"])
def test_c9_1_frontend_never_invents_percentage_without_provider_evidence(probe, tmp_path, label):
    """C-9.1 non-provider readings cannot become percentages in either provider's menu row."""
    result = display(probe, tmp_path, [lane("codex", label=label), lane("claude", label=label)])
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert row["percentage"] is None and row["weekly_percentage"] is None
        assert row["five_hour_reset"] is None and row["weekly_reset"] is None
        if label != "admission-observed":
            assert row["tone"] != "good"


@pytest.mark.parametrize("condition", ["stale-reading", "stale-snapshot", "offline"])
def test_c9_1_frontend_marks_cached_evidence_stale(probe, tmp_path, condition):
    """C-9.1 stale provider data stays labelled stale rather than looking like fresh live capacity."""
    label = "stale-provider" if condition == "stale-reading" else "provider"
    result = display(probe, tmp_path, [lane("codex", label=label), lane("claude", label=label)],
                     offline=condition == "offline",
                     generated_at=NOW - timedelta(minutes=11) if condition == "stale-snapshot" else NOW)
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert row["stale"] is True
        assert row["tone"] != "good"


def test_c10_frontend_does_not_present_v1_lanes_as_available(probe, tmp_path):
    """C-10.4 ownership stays visible; a lane still assigned to v1 is not shown as available."""
    result = display(probe, tmp_path, [lane("codex", owner="v1"), lane("claude", owner="v1")])
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert "v1" in (row["status"] + " " + row["detail"]).lower()
        assert row["tone"] != "good"


def test_c10_6_frontend_suppresses_mismatched_identity_percentages(probe, tmp_path):
    """C-10.6 a mismatched lane cannot advertise another account's observed capacity."""
    result = display(probe, tmp_path, [lane("codex", identity_status="mismatch"),
                                      lane("claude", identity_status="mismatch")])
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert row["percentage"] is None and row["weekly_percentage"] is None
        assert row["five_hour_reset"] is None and row["weekly_reset"] is None
        assert "mismatch" in (row["status"] + " " + row["detail"]).lower()
        assert row["tone"] == "error"


def test_c2_1_frontend_resolves_custom_state_root(probe, tmp_path):
    """C-2.1 the app resolves SUBFLEET_HOME independently of the historical v1 location."""
    home = tmp_path / "home"
    for override, expected in [(None, home / ".subfleet/status.json"),
                               (str(tmp_path / "custom state"), tmp_path / "custom state/status.json"),
                               ("~/custom-state", home / "custom-state/status.json")]:
        args = [str(probe), "path", str(home)]
        if override is not None:
            args.append(override)
        result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=10)
        assert result.stdout.strip() == str(expected)


def test_c18_1_frontend_accepts_empty_fleet(probe, tmp_path):
    """C-18.1 an empty initial daemon snapshot decodes without inventing lanes or capacity."""
    result = display(probe, tmp_path, [])
    assert result == {"stale": False, "codex": [], "claude": [],
                      "has_jobs_section": True, "job_groups": [], "recent_jobs": [],
                      "dispatchable": {"claude": 0, "codex": 0}, "auto_provider": "claude"}


def test_c9_1_frontend_handles_lanes_without_usage_windows(probe, tmp_path):
    """C-9.1 newly enrolled lanes without any readings display unknown rather than zero usage."""
    result = display(probe, tmp_path, [lane("codex", readings=[]), lane("claude", readings=[])])
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert row["percentage"] is None and row["weekly_percentage"] is None
        assert row["tone"] != "good"


@pytest.mark.parametrize("timestamp,stale", [
    ("2026-09-19T12:00:00.123Z", False),
    ("2026-09-19T11:50:00Z", False),
    ("2026-09-19T11:49:59Z", True),
    ("2026-09-19T12:01:01Z", True),
    ("invalid-clock", True),
])
def test_c18_1_frontend_snapshot_clock_is_conservative(probe, tmp_path, timestamp, stale):
    """C-18.1 stale, future and invalid snapshot clocks cannot present provider data as live."""
    payload = build_status({"lanes": [lane("codex"), lane("claude")]}, now=NOW)
    payload["generated_at"] = timestamp
    result = project(probe, tmp_path, payload)
    assert result["stale"] is stale
    for provider in ("codex", "claude"):
        assert result[provider][0]["stale"] is stale


def _job(job_id, state, **extra):
    return {"job_id": job_id, "state": state, "name": extra.pop("name", job_id), "sandbox": "workspace-write",
            "workdir": f"/work/{job_id}", "pinned_model": "fable", "created_at": "2026-09-19T11:00:00Z", **extra}


def test_c18_2_frontend_groups_a_batch_and_says_why_a_job_waits(probe, tmp_path):
    """C-17.7, C-18.2 the Swift model keeps a batch together and shows a waiting job's reason."""
    batch = {"id": "h-0920", "label": "codex handoff", "size": 3}
    jobs = [_job("a", "running", name="spm"), _job("solo", "running", name=""),
            _job("b", "waiting", name="tariff", wait_reason="workspace"),
            _job("c", "waiting", name="thesis", wait_reason="capacity"),
            _job("old", "failed", rc=7, finished_at="2026-09-19T10:00:00Z")]
    attempts = [{"job_id": "a", "seq": 1, "lane_id": "claude-13", "model_requested": "claude-fable-5-1"}]
    payload = build_status({"lanes": [], "jobs": jobs, "attempts": attempts,
                            "batches": {key: {**batch, "index": n} for n, key in enumerate("abc", 1)}}, now=NOW)
    result = project(probe, tmp_path, payload)
    assert result["has_jobs_section"] is True
    groups = result["job_groups"]
    assert [group["title"] for group in groups] == ["codex handoff · 3 jobs", None]
    assert [job["title"] for job in groups[0]["jobs"]] == ["spm", "tariff", "thesis"]
    assert groups[0]["jobs"][0] == {"title": "spm", "detail": "claude-fable-5-1 · claude-13 · a",
                                   "status": "running", "tone": "good"}
    assert [(job["status"], job["tone"]) for job in groups[0]["jobs"][1:]] == [
        ("waiting: workspace", "warning"), ("waiting: capacity", "neutral")]
    assert groups[1]["jobs"][0]["title"] == "solo"            # an unnamed job shows its id
    assert result["recent_jobs"] == [{"title": "old", "detail": "fable · old", "status": "failed rc 7", "tone": "error"}]


def test_c18_2_frontend_reads_a_snapshot_from_a_daemon_without_jobs(probe, tmp_path):
    """C-18.2 an older daemon's status.json has no jobs key, and the menu still decodes it."""
    payload = build_status({"lanes": [lane("codex")]}, now=NOW)
    payload.pop("jobs")
    result = project(probe, tmp_path, payload)
    assert result["has_jobs_section"] is False and result["job_groups"] == [] and result["recent_jobs"] == []
    assert result["codex"][0]["percentage"] == 25


def test_c29_6_frontend_decodes_the_conversation_window_and_kind_additions(probe, tmp_path):
    """C-29.6, C-18.2 (IR-18, IR-34): today's menu model decodes the added keys; a turn job is not
    among its jobs, and a nearly full Fable window does not become the account's weekly percentage."""
    claude = lane("claude")
    claude["readings"].append({"scope": "claude-fable-5-1", "window": "seven_day", "label": "provider",
                               "source": "fixture", "utilization": .95, "observed_at": NOW.isoformat(),
                               "resets_at": (NOW + timedelta(days=2)).isoformat()})
    jobs = [_job("a", "running", kind="dispatch"), _job("t", "running", kind="turn", name="turn-cv-1")]
    summary = {"available": True, "counts": {"active": 1, "needs_approval": 1, "blocked": 0}, "truncated": False,
               "items": [{"conversation_id": "cv-1", "provider": "claude", "title": "a", "state": "approval-needed",
                          "blocked_by": None, "pending_approvals": 1, "updated_at": NOW.isoformat()}]}
    payload = build_status({"lanes": [claude], "jobs": jobs, "conversations": summary,
                            "model_names": {"claude-fable-5-1": "fable"}}, now=NOW)
    assert [(w["scope"], w["model"]) for w in payload["claude"]["accounts"][0]["windows"]] == [
        ("account", None), ("account", None), ("claude-fable-5-1", "fable")]
    assert payload["conversations"]["items"][0]["turn"]["job_id"] == "t"
    result = project(probe, tmp_path, payload)
    assert result["claude"][0]["percentage"] == 25 and result["claude"][0]["weekly_percentage"] == 60
    assert [job["title"] for group in result["job_groups"] for job in group["jobs"]] == ["a"]


def test_auto_provider_follows_the_lanes_ready_now(probe, tmp_path):
    """A new conversation's Auto choice: the provider with more lanes admission
    could place work on now, from the daemon's own status.json; Claude on a tie."""
    payload = build_status({"lanes": [lane("codex"), lane("claude")], "offline": False}, now=NOW)
    payload["claude"]["lanes"] = {"dispatchable_now": 1}
    payload["codex"]["fleet"]["dispatchable_now"] = 1
    tie = project(probe, tmp_path, payload)
    assert tie["dispatchable"] == {"claude": 1, "codex": 1} and tie["auto_provider"] == "claude"
    payload["codex"]["fleet"]["dispatchable_now"] = 4
    assert project(probe, tmp_path, payload)["auto_provider"] == "codex"
    del payload["claude"]["lanes"]
    payload["codex"]["fleet"]["dispatchable_now"] = 0
    unknown = project(probe, tmp_path, payload)
    assert unknown["dispatchable"] == {"codex": 0} and unknown["auto_provider"] == "claude"


def test_c18_1_frontend_reads_a_lane_a_probe_holds_as_unavailable(probe, tmp_path):
    """C-5.7a, C-18.1 the probe fields are additions the Swift model decodes past, and a held lane reads unavailable."""
    result = display(probe, tmp_path, [lane("codex", probe_state="quarantined", probe_holder="probe:q1"),
                                       lane("claude", probe_state="reserved", probe_holder="probe:q2")])
    for provider in ("codex", "claude"):
        row = result[provider][0]
        assert row["status"] == "Unavailable" and row["tone"] == "neutral"
        assert "Not dispatchable" in row["detail"]
        assert row["percentage"] == 25 and row["weekly_percentage"] == 60
