"""Native resume uses durable provider identity, without launching any processes."""

import json
from dataclasses import replace

import pytest

from subfleet import daemon as module, protocol
from subfleet.adapters.base import AdapterError
from subfleet.adapters.registry import register
from subfleet.contracts import Credential, Reading, ReadingLabel
from subfleet.daemon import Daemon, after, native_session_lease_key, utcnow
from subfleet.procs import Containment
from subfleet.retention import maintenance
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon
from tests.fake.test_workspace_contract import repository
from tests.fake_adapter import FakeAdapter


def finished_source(daemon, harness, *, writable=False, cancelled=False, **overrides):
    if writable:
        repository(daemon, harness)
    job_id, attempt, adir = reserve(daemon, harness,
        sandbox="workspace-write" if writable else "read-only", **overrides)
    daemon.store.update_attempt(attempt["attempt_id"], native_session_id="native-source-session")
    if cancelled:
        daemon.kill(protocol.KillArgs(job_id))
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    return job_id, daemon.store.get_attempt(attempt["attempt_id"])


@pytest.mark.parametrize("provider,model", [("codex", "astra"), ("claude", "haiku")])
def test_c12_resume_invokes_native_launch_on_original_lane(state_daemon, monkeypatch, provider, model):
    """C-12.3, C-12.4 resumes call the adapter's native continuation with the frozen source session."""
    daemon, harness = state_daemon
    if provider == "claude":
        lane = daemon.store.get_lane("codex-1")
        daemon.store.put_lane(replace(lane, lane_id="claude-1", provider="claude",
            account_key="claude:fixture", credential=Credential("claude", lane.home, "home")))
        daemon.desktop_prober = lambda: None
        register("claude", FakeAdapter)
    source_id, source_attempt = finished_source(daemon, harness, pinned_model=model,
                                               pinned_lane=f"{provider}-1")
    daemon.store.update_job(source_id, exclusions=json.dumps(["unrelated-excluded-lane"]))
    resumed_id = daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume",
        parent_job_id=source_id, pinned_lane="wrong-caller-lane", pinned_model="wrong-caller-model")))["job_id"]
    resumed = daemon.store.get_job(resumed_id)
    assert resumed["pinned_lane"] == source_attempt["lane_id"]
    assert json.loads(resumed["exclusions"]) == ["unrelated-excluded-lane"]
    assert daemon.policy["models"][resumed["pinned_model"]]["id"] == source_attempt["model_requested"]
    manifest = json.loads((daemon.root / "jobs" / resumed_id / "manifest.json").read_text())
    assert manifest["resume"]["native_session_id"] == "native-source-session"
    # The launch uses the submitted identity, not a later reread of the source.
    daemon.store.update_attempt(source_attempt["attempt_id"], native_session_id="later-unrelated-session")
    daemon._admit()
    attempt = daemon.store.list_attempts(resumed_id)[0]
    calls = []

    class ObserveResume(FakeAdapter):
        def resume_launch(self, spec, aid, adir, lane, env, native, prompt, guard, model_id=None):
            calls.append((lane.lane_id, native, model_id, spec.workdir))
            raise AdapterError("stopped after observing native launch")

        def build_launch(self, *args):
            raise AssertionError("a resume must not start a fresh provider session")

    monkeypatch.setattr(module, "get_adapter", lambda _: ObserveResume())
    Daemon._launch(daemon, attempt)
    assert calls == [(source_attempt["lane_id"], "native-source-session",
                      source_attempt["model_requested"], resumed["workdir"])]
    assert daemon._children == {}


@pytest.mark.parametrize("when", ["before-submit", "while-queued"])
def test_c12_resume_follows_its_lane_through_a_reenrolment(state_daemon, monkeypatch, when):
    """C-12.3, C-11.2 a re-enrolment binds the same credential, and so the same native sessions, to a new
    lane id; a resume routed there by the pin's re-enrolment rule is launched there, not refused at spawn."""
    daemon, harness = state_daemon
    source_id, source_attempt = finished_source(daemon, harness, pinned_model="astra", pinned_lane="codex-1")

    def reenrol():
        daemon.store.update_lane("codex-1", enabled=0)
        daemon.store.put_lane(replace(daemon.store.get_lane("codex-1"), lane_id="codex-9", enabled=True))
    if when == "before-submit":
        reenrol()
    resumed_id = daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume", parent_job_id=source_id)))["job_id"]
    manifest = json.loads((daemon.root / "jobs" / resumed_id / "manifest.json").read_text())
    assert manifest["resume"]["lane_id"] == "codex-1"                # the request as recorded (its digest) is untouched
    if when == "while-queued":
        reenrol()
    daemon._admit()
    attempt = daemon.store.list_attempts(resumed_id)[0]
    assert attempt["lane_id"] == "codex-9"
    calls = []

    class ObserveResume(FakeAdapter):
        def resume_launch(self, spec, aid, adir, lane, env, native, prompt, guard, model_id=None):
            calls.append((lane.lane_id, native))
            raise AdapterError("stopped after observing native launch")

    monkeypatch.setattr(module, "get_adapter", lambda _: ObserveResume())
    Daemon._launch(daemon, attempt)
    assert calls == [("codex-9", "native-source-session")]


def test_c12_a_resume_never_launches_on_an_unrelated_lane(state_daemon, monkeypatch):
    """C-12.3 the re-enrolment rule is the only way a resume leaves its recorded lane."""
    daemon, harness = state_daemon
    source_id, _ = finished_source(daemon, harness, pinned_model="astra", pinned_lane="codex-1")
    resumed_id = daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume", parent_job_id=source_id)))["job_id"]
    daemon.store.put_lane(replace(daemon.store.get_lane("codex-1"), lane_id="codex-7", home="/elsewhere",
                                  credential=Credential("codex", "/elsewhere", "home")))
    daemon._admit()
    assert not daemon._resume_lane("codex-1", daemon.store.get_lane("codex-7"))
    assert daemon._resume_lane("codex-1", daemon.store.get_lane("codex-1"))


def test_c13_resume_reuses_cancelled_source_allocated_worktree(state_daemon):
    """C-7.3, C-13.3 a cancelled job continues in its preserved execution workspace."""
    daemon, harness = state_daemon
    source_id, source_attempt = finished_source(daemon, harness, writable=True, cancelled=True)
    source = daemon.store.get_job(source_id)
    assert source["worktree"] != source["workdir"]
    resumed_id = daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume",
        parent_job_id=source_id)))["job_id"]
    resumed = daemon.store.get_job(resumed_id)
    assert resumed["workdir"] == source["worktree"]
    assert resumed["sandbox"] == "workspace-write" and resumed["in_place"]
    assert resumed["independent"] and resumed["cancel_requested_at"] is None
    daemon._admit()
    assert daemon.store.get_job(resumed_id)["worktree"] == source["worktree"]
    assert daemon.store.list_attempts(resumed_id)[0]["lane_id"] == source_attempt["lane_id"]


def test_c13_4_resume_protects_source_worktree_from_retention(state_daemon):
    """C-13.4 continuation pins its source's allocated workspace while queued and after admission."""
    daemon, harness = state_daemon
    source_id, _ = finished_source(daemon, harness, writable=True)
    source = daemon.store.get_job(source_id)
    daemon.store.acknowledge_notices("fake-session", [row["notice_id"] for row in daemon.store.list_notices()])
    resumed_id = daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume",
        parent_job_id=source_id)))["job_id"]
    for state in ("queued", "running"):
        if state == "running":
            daemon._admit()
        assert daemon.store.get_job(resumed_id)["state"] == state
        report = maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0)
        assert source_id in report["protected"]
        assert source_id not in report["pruned"]
        assert daemon.store.get_job(source_id)
        assert (daemon.root / "worktrees" / source_id).is_dir()
        assert daemon.store.get_job(resumed_id)["workdir"] == source["worktree"]


@pytest.mark.parametrize("defect", ["active", "no-session", "isolated"])
def test_c12_resume_refuses_source_without_valid_native_context(state_daemon, defect):
    """C-12.3, C-12.4, C-23.2 missing, active and isolated native contexts never become new sessions."""
    daemon, harness = state_daemon
    source_id, attempt = finished_source(daemon, harness)
    if defect == "active":
        # A fabricated row: a finished job is never running again, so its
        # notice (written when it finished) cannot match it (C-15.1 check off).
        harness.notice_check = False
        daemon.store.update_job(source_id, state="running")
    elif defect == "no-session":
        daemon.store.update_attempt(attempt["attempt_id"], native_session_id=None)
    else:
        daemon.store.update_job(source_id, isolated_review=1)
    with pytest.raises(AdapterError) as error:
        daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume", parent_job_id=source_id)))
    assert error.value.code == 7 and error.value.fix
    assert len(daemon.store.list_jobs()) == 1


def test_c6_2_resume_request_digest_binds_source_identity(state_daemon):
    """C-6.2 the same request id cannot resume a different source with an otherwise identical prompt."""
    daemon, harness = state_daemon
    first, _ = finished_source(daemon, harness)
    second, _ = finished_source(daemon, harness)
    args = protocol.SubmitArgs(**harness.submit_args(kind="resume", parent_job_id=first))
    original = daemon.submit(args)
    assert daemon.submit(args)["job_id"] == original["job_id"]
    with pytest.raises(protocol.ProtocolError, match="different payload"):
        daemon.submit(replace(args, parent_job_id=second))


@pytest.mark.parametrize("identity_source", ["stderr", "rollout", "conflict"])
def test_c23_32_legacy_resume_resolves_identity_without_rewriting_source(state_daemon, identity_source):
    """C-23.32 imported legacy Codex identity is recovered in memory and conflicting evidence is refused."""
    daemon, harness = state_daemon
    source_id, attempt = finished_source(daemon, harness)
    native = "01a07281-5833-72f1-b299-1537f2ae87d0"
    fields = {"native_session_id": None, "evidence_json": json.dumps({"imported": True})}
    stderr = daemon.root / "jobs" / source_id / "a1" / "stderr"
    if identity_source in ("stderr", "conflict"):
        stderr.write_text(f"session id: {native}\n")
    if identity_source in ("rollout", "conflict"):
        rollout_id = native if identity_source == "rollout" else "00000000-0000-4000-8000-000000000001"
        fields["transcript_path"] = f"/legacy/rollout-2026-09-05T11-18-55-{rollout_id}.jsonl"
    daemon.store.update_attempt(attempt["attempt_id"], **fields)
    original_attempt = daemon.store.get_attempt(attempt["attempt_id"])
    original_stderr = stderr.read_bytes()
    args = protocol.SubmitArgs(**harness.submit_args(kind="resume", parent_job_id=source_id))
    if identity_source == "conflict":
        with pytest.raises(AdapterError, match="no recorded native session"):
            daemon.submit(args)
    else:
        result = daemon.submit(args)
        manifest = json.loads((daemon.root / "jobs" / result["job_id"] / "manifest.json").read_text())
        assert manifest["resume"]["native_session_id"] == native
    assert daemon.store.get_attempt(attempt["attempt_id"]) == original_attempt
    assert stderr.read_bytes() == original_stderr


def measured_lane(daemon, lane_id="codex-1"):
    # Two lane slots must be available: otherwise the ordinary capacity limit
    # could mask a missing lock on the shared provider transcript.
    daemon.store.add_reading(Reading(lane_id, "account", "seven_day", .1,
                                    after(3600), ReadingLabel.PROVIDER, "fixture", utcnow()))


def resume_job(daemon, harness, source_id):
    return daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume",
        parent_job_id=source_id)))["job_id"]


def finish_reserved(daemon, job_id):
    attempt = daemon.store.list_attempts(job_id)[0]
    daemon._pending_launches.discard(attempt["attempt_id"])
    adir = daemon.root / "jobs" / job_id / "a1"
    adir.mkdir()
    daemon._finalize(receipt_fixture(daemon, attempt, adir))


def test_c12_readonly_resumes_serialize_one_native_transcript(state_daemon):
    """C-6.3, C-12.3, C-12.6 read-only resumes cannot overlap writes to the same native transcript."""
    daemon, harness = state_daemon
    measured_lane(daemon)
    source_id, source_attempt = finished_source(daemon, harness)
    first = resume_job(daemon, harness, source_id)
    second = resume_job(daemon, harness, source_id)
    daemon._admit()
    assert daemon.store.get_job(first)["state"] == "running"
    assert daemon.store.get_job(second)["state"] == "waiting"
    assert daemon.store.list_attempts(second) == []
    key = native_session_lease_key(source_attempt["lane_id"], "native-source-session")
    assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))["holder"] == first
    finish_reserved(daemon, first)
    assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?", (key,)) is None
    daemon.store.update_job(second, next_check_at=None)
    daemon._admit()
    assert daemon.store.get_job(second)["state"] == "running"
    assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))["holder"] == second


def test_c12_readonly_resumes_allow_distinct_native_sessions(state_daemon):
    """C-6.3, C-12.3 native-session leases preserve lane concurrency for distinct sessions."""
    daemon, harness = state_daemon
    measured_lane(daemon)
    first_source, _ = finished_source(daemon, harness)
    second_source, second_attempt = finished_source(daemon, harness)
    daemon.store.update_attempt(second_attempt["attempt_id"], native_session_id="different-native-session")
    jobs = [resume_job(daemon, harness, source) for source in (first_source, second_source)]
    daemon._admit()
    assert [daemon.store.get_job(job)["state"] for job in jobs] == ["running", "running"]
    assert len(daemon.store.query("SELECT * FROM leases WHERE lease_key LIKE 'native-session:%'")) == 2


def test_c5_7_quarantine_retains_native_session_lease(state_daemon):
    """C-5.7, C-12.6 uncontained native writers keep the transcript lease until quarantine resolution."""
    daemon, harness = state_daemon
    measured_lane(daemon)
    source_id, source_attempt = finished_source(daemon, harness)
    first = resume_job(daemon, harness, source_id)
    second = resume_job(daemon, harness, source_id)
    daemon._admit()
    attempt = daemon.store.list_attempts(first)[0]
    daemon._quarantine(attempt, Containment(marker_pids=frozenset({42099})), "fixture survivor")
    key = native_session_lease_key(source_attempt["lane_id"], "native-source-session")
    assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))["holder"] == first
    daemon.store.update_job(second, next_check_at=None)
    daemon._admit()
    assert daemon.store.get_job(second)["state"] == "waiting"
    assert daemon.store.list_attempts(second) == []
    daemon._resolve_quarantine(attempt, protocol.KillArgs(first, confirm_dead=True))
    daemon.store.update_job(second, next_check_at=None)
    daemon._admit()
    assert daemon.store.get_job(second)["state"] == "running"


@pytest.mark.parametrize("first_kind", ["resume", "revive"])
@pytest.mark.parametrize("provider,model", [("codex", "astra"), ("claude", "haiku")])
def test_c12_resume_and_revive_share_native_session_lease(state_daemon, monkeypatch, first_kind, provider, model):
    """C-12.3/4, C-23.54 native revival and ordinary resume cannot continue one session concurrently."""
    daemon, harness = state_daemon
    lane_id = f"{provider}-1"
    if provider == "claude":
        lane = daemon.store.get_lane("codex-1")
        daemon.store.put_lane(replace(lane, lane_id=lane_id, provider="claude",
            account_key="claude:fixture", credential=Credential("claude", lane.home, "home")))
        daemon.desktop_prober = lambda: None
        register("claude", FakeAdapter)
    measured_lane(daemon, lane_id)
    source_id, _ = finished_source(daemon, harness, pinned_model=model, pinned_lane=lane_id)
    # Model successful same-pass probe preparation without spawning a guardian;
    # admission still evaluates the real scheduler and acquires real SQL leases.
    monkeypatch.setattr(daemon, "_prepare_route", lambda *_: ({(lane_id, model)}, daemon._desktop_identity()))
    jobs = []
    for kind in (first_kind, "revive" if first_kind == "resume" else "resume"):
        if kind == "resume":
            jobs.append(resume_job(daemon, harness, source_id))
        else:
            jobs.append(daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="revive",
                caller_session="native-source-session", pinned_model=model, pinned_lane=lane_id)))["job_id"])
    daemon._admit()
    assert daemon.store.get_job(jobs[0])["state"] == "running"
    assert daemon.store.get_job(jobs[1])["state"] == "waiting"
    assert daemon.store.list_attempts(jobs[1]) == []
