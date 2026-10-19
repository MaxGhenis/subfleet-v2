"""C-11.1, C-12.3/4, C-14.2 end to end: the shipped `review: worktree-write`
default, through the real CLI, daemon, guard preflight, guardian and adapters.

A policy review of a committed repository runs writable in its own job
worktree. On Codex the guard preflight verifies the publish hook beside the
never-rules guard (the fake app-server lists every group the override trusts,
as Codex 0.159.0 lists a second trusted group); on both providers the launch
self-tests the installed publish hook before it starts, and the provider gets
the git denial and the launch marker through D-ENV1's selection and the
guardian's. A read-only Codex review carries the publish hook too
(`test_capacity_steer_e2e.py` submits one).
"""

from __future__ import annotations

import json
from pathlib import Path
import tomllib

import pytest

from subfleet import refusals
from subfleet.guard.preflight import HOOK_KEY, PUBLISH_HOOK_KEY, PUBLISH_HOOK_STATUS
from subfleet.review_publish import pinned_environment

PINNED = pinned_environment()


@pytest.mark.parametrize("model,scenario,provider", [
    ("astra", "success", "codex"),
    ("haiku", "success-allowed", "claude"),
])
def test_a_policy_review_runs_writable_with_its_publish_layers(e2e, model, scenario, provider):
    e2e.start(scenario=scenario)
    result = e2e.cli(*e2e.run_args(model, "--task", "review", "--tier", "standard", "--wait"), timeout=120)
    assert result.rc == 0, result
    job_id = result.stdout.strip().splitlines()[0].strip()
    job = e2e.job(job_id)
    assert job["sandbox"] == "workspace-write" and job["worktree"], job
    attempt, = e2e.attempts(job_id)
    assert (attempt["state"], attempt["capability"], attempt["publish_denied"]) == ("succeeded", "writable", 1)
    directory = e2e.root / "jobs" / attempt["attempt_id"]
    launch = json.loads((directory / "launch.json").read_text())
    # C-12.4: the installed hook denied a gh write before the provider started.
    assert [verdict["ok"] for verdict in launch["notes"]["publish_hook_self_test"]] == [True]
    if provider == "codex":
        preflight = json.loads((directory / "guard-preflight.json").read_text())
        assert preflight["ok"] and preflight["kind"] == "verified", preflight
        override = next(arg for arg in launch["argv"] if arg.startswith("hooks="))
        hooks = tomllib.loads(override)["hooks"]
        assert set(hooks["state"]) == {HOOK_KEY, PUBLISH_HOOK_KEY}
        assert hooks["PreToolUse"][1]["hooks"][0]["statusMessage"] == PUBLISH_HOOK_STATUS
        report = json.loads((e2e.root / "codex-env.json").read_text())
        assert {name: report["env"][name] for name in PINNED} == PINNED
        assert report["argv"][report["argv"].index("--sandbox") + 1] == "workspace-write"
    else:
        report = json.loads((e2e.root / "claude-env.json").read_text())
        assert set(PINNED) <= set(report["env_keys"])
        settings = report["argv"].index("--settings")
        assert report["argv"][settings - 2:settings] == ["--setting-sources", "user"]
    assert Path(report["cwd"]).resolve() == Path(job["worktree"]).resolve()


@pytest.mark.parametrize("sandbox", [(), ("-s", "read-only")], ids=["policy", "read-only"])
def test_a_publish_hook_codex_does_not_trust_refuses_the_review_with_its_own_advice(e2e, sandbox):
    """C-14.2 (D-SH1 round 8): the guard verified and only the publish group did not, in
    either sandbox, so the refusal names the review's hook and how to see why; it neither
    calls the job writable nor sends the operator to doctor, which never checks this hook."""
    e2e.start(env={"SUBFLEET_FAKE_PUBLISH_UNTRUSTED": "1"})
    refused = e2e.cli(*e2e.run_args("astra", "--task", "review", "--tier", "standard", *sandbox, "--wait"),
                      timeout=120)
    assert refused.rc == 7, refused
    message, fix = refusals.render("guard-preflight-publish")
    assert message in refused.stderr and fix in refused.stderr, refused
    assert "rerun subfleet doctor" not in refused.stderr and "writable job" not in refused.stderr
    assert not (e2e.root / "codex-env.json").exists()
    for attempt in e2e.rows("SELECT * FROM attempts"):
        assert attempt["guardian_pid"] is None
