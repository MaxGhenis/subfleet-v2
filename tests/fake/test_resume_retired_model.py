"""C-11.1, C-12.3: a resume of a session last served by a model the running policy has
since retired continues on the successor admission routes it to.

Found on the release line by the adversarial review of PR #66 (Fable's retirement there),
and present on main since #58: the resume manifest keeps the source attempt's model id
(`claude-fable-5-1`), admission resolves that pin through the policy's `retired` map to
Opus, and the launch then refused the pair as a mismatch, exit 7, with a fix (resubmit
from the original job) that repeats it.
"""

import json
from dataclasses import replace

import pytest

from subfleet import daemon as module, protocol
from subfleet.adapters.base import AdapterError
from subfleet.adapters.registry import register
from subfleet.contracts import Credential
from subfleet.daemon import Daemon
from tests.fake.test_resume_contract import finished_source
from tests.fake.test_state_contract import state_daemon  # noqa: F401 (a fixture)
from tests.fake_adapter import FakeAdapter

FABLE = {"provider": "claude", "id": "claude-fable-5-1", "priority": 4}
FABLE_IDS = ("fable", "claude-fable-5", "claude-fable-5-1")


def claude_lane(daemon):
    lane = daemon.store.get_lane("codex-1")
    daemon.store.put_lane(replace(lane, lane_id="claude-1", provider="claude",
                                  account_key="claude:fixture", credential=Credential("claude", lane.home, "home")))
    daemon.desktop_prober = lambda: None
    register("claude", FakeAdapter)


def fable_source(daemon, harness):
    """A finished Claude job that ran on Fable while the policy still listed it."""
    daemon.policy["models"]["fable"] = dict(FABLE)
    source_id, attempt = finished_source(daemon, harness, pinned_model="fable", pinned_lane="claude-1")
    assert attempt["model_requested"] == "claude-fable-5-1"
    return source_id, attempt


def launched_model(daemon, monkeypatch, resumed_id):
    """The model id the resume's launch asks the adapter for, or the refusal it ends with."""
    daemon._admit()
    attempt = daemon.store.list_attempts(resumed_id)[0]
    calls, failures = [], []

    class ObserveResume(FakeAdapter):
        def resume_launch(self, spec, aid, adir, lane, env, native, prompt, guard, model_id=None):
            calls.append((native, model_id))
            raise AdapterError("stopped after observing native launch")

        def build_launch(self, *args):
            raise AssertionError("a resume must not start a fresh provider session")

    monkeypatch.setattr(module, "get_adapter", lambda _: ObserveResume())
    original = daemon._launch_failure
    monkeypatch.setattr(daemon, "_launch_failure", lambda a, why, rc=None: (failures.append(why), original(a, why, rc=rc)))
    Daemon._launch(daemon, attempt)
    return calls, failures


def test_a_fable_session_resumes_on_opus_once_the_policy_retires_fable(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    claude_lane(daemon)
    source_id, attempt = fable_source(daemon, harness)
    # The upgrade: the running policy retires Fable, as the shipped one does.
    del daemon.policy["models"]["fable"]
    daemon.policy["retired"] = {**daemon.policy.get("retired", {}), **{name: "opus" for name in FABLE_IDS}}
    resumed_id = daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume", parent_job_id=source_id)))["job_id"]
    manifest = json.loads((daemon.root / "jobs" / resumed_id / "manifest.json").read_text())
    assert manifest["resume"]["model_id"] == "claude-fable-5-1"          # the source is kept as recorded
    calls, failures = launched_model(daemon, monkeypatch, resumed_id)
    assert calls == [("native-source-session", daemon.policy["models"]["opus"]["id"])]
    assert not [why for why in failures if "does not match this attempt" in why]


def test_under_a_policy_that_still_lists_fable_a_fable_session_resumes_on_fable(state_daemon, monkeypatch):
    """The running policy decides (d574): the live policy of 2026-09-29 still lists Fable,
    and its `retired` map sends only `claude-fable-5` there, not to Opus."""
    daemon, harness = state_daemon
    claude_lane(daemon)
    source_id, _ = fable_source(daemon, harness)
    daemon.policy["retired"] = {"sol": "astra", "claude-fable-5": "fable"}
    resumed_id = daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume", parent_job_id=source_id)))["job_id"]
    calls, _ = launched_model(daemon, monkeypatch, resumed_id)
    assert calls == [("native-source-session", "claude-fable-5-1")]


def test_a_resume_still_refuses_a_model_its_source_does_not_resolve_to(state_daemon, monkeypatch):
    """The check keeps its teeth: admission on any model other than the one the source's
    model resolves to under the running policy is refused, as before."""
    daemon, harness = state_daemon
    claude_lane(daemon)
    source_id, _ = fable_source(daemon, harness)
    resumed_id = daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume", parent_job_id=source_id)))["job_id"]
    manifest_path = daemon.root / "jobs" / resumed_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["resume"]["model_id"] = "claude-haiku-4-5-20251001"          # a source on another model
    manifest_path.write_text(json.dumps(manifest))
    calls, failures = launched_model(daemon, monkeypatch, resumed_id)
    assert calls == []
    assert any("does not match this attempt" in why for why in failures)
