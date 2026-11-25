"""Native resume uses durable provider identity, without launching any processes."""

import json
from dataclasses import replace

import pytest

from subfleet import daemon as module, protocol
from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.registry import register
from subfleet.contracts import Credential, Reading, ReadingLabel
from subfleet.daemon import Daemon, after, native_session_lease_key, utcnow
from subfleet.procs import Containment
from subfleet.policy import load_policy
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


@pytest.mark.parametrize("effort", [None, "medium"])
@pytest.mark.parametrize("provider,model", [("codex", "astra"), ("claude", "haiku")])
def test_c12_resume_invokes_native_launch_on_original_lane(state_daemon, monkeypatch, provider, model, effort):
    """C-12.3, C-12.4 resumes call the adapter's native continuation with the frozen source session,
    and with the effort the attempt's model entry names, as a fresh launch gets it (D-EFFORT-ENV1)."""
    daemon, harness = state_daemon
    if effort is None:
        monkeypatch.delitem(daemon.policy["models"][model], "effort", raising=False)
    else:
        monkeypatch.setitem(daemon.policy["models"][model], "effort", effort)
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
        def resume_launch(self, spec, aid, adir, lane, env, native, prompt, guard, model_id=None, effort=None):
            calls.append((lane.lane_id, native, model_id, spec.workdir, effort))
            raise AdapterError("stopped after observing native launch")

        def build_launch(self, *args):
            raise AssertionError("a resume must not start a fresh provider session")

    monkeypatch.setattr(module, "get_adapter", lambda _: ObserveResume())
    Daemon._launch(daemon, attempt)
    assert calls == [(source_attempt["lane_id"], "native-source-session",
                      source_attempt["model_requested"], resumed["workdir"], effort)]
    assert daemon._children == {}


@pytest.mark.parametrize("effort", [None, "high"])
@pytest.mark.parametrize("source_route", ["pin", "task"])
def test_resume_keeps_source_effort_when_policy_aliases_share_a_model_id(
        state_daemon, monkeypatch, effort, source_route):
    """C-12.4, C-6.2: preserve the source alias and pre-upgrade accepted retries."""
    daemon, harness = state_daemon
    daemon.policy["models"]["opus"]["effort"] = "low"
    alternate = {**daemon.policy["models"]["opus"]}
    alternate.pop("effort")
    if effort is not None:
        alternate["effort"] = effort
    daemon.policy["models"]["opus_alternate"] = alternate
    daemon.policy["chains"]["lookup"] = ["opus_alternate"] * len(daemon.policy["tiers"])
    policy_path = daemon.root / "policy.json"
    policy_path.write_text(json.dumps(daemon.policy))
    daemon.policy = load_policy(policy_path)
    lane = daemon.store.get_lane("codex-1")
    daemon.store.put_lane(replace(lane, lane_id="claude-1", provider="claude",
        account_key="claude:fixture", credential=Credential("claude", lane.home, "home")))
    daemon.desktop_prober = lambda: None
    register("claude", FakeAdapter)
    source_id, source_attempt = finished_source(daemon, harness, task="lookup",
        pinned_lane="claude-1", pinned_model="opus_alternate" if source_route == "pin" else None)
    assert json.loads(source_attempt["evidence_json"])["model_short"] == "opus_alternate"
    adapter = ClaudeAdapter()
    source_job = daemon.store.get_job(source_id)
    source_dir = daemon.root / "jobs" / source_id / "a1"
    fresh = adapter.build_launch(daemon._spec(source_job), source_attempt["attempt_id"],
        source_dir, daemon.store.get_lane("claude-1"), {}, alternate["id"], effort,
        source_dir / "stdout", None)
    # Simulate the digest an older daemon accepted: it resolved the provider
    # model ID to the first alias, even when the source selected the alternate.
    payload_digest = module.ids.payload_digest
    legacy_digests = []

    def capture_legacy_digest(prompt, **payload):
        legacy_digests.append(payload_digest(prompt, **{**payload, "pinned_model": "opus"}))
        return payload_digest(prompt, **payload)

    resume_args = protocol.SubmitArgs(**harness.submit_args(kind="resume", parent_job_id=source_id))
    with monkeypatch.context() as capture:
        capture.setattr(module.ids, "payload_digest", capture_legacy_digest)
        resumed_id = daemon.submit(resume_args)["job_id"]
    assert len(legacy_digests) == 1
    daemon.store.update_job(resumed_id, payload_digest=legacy_digests[0])
    retried = daemon.submit(resume_args)
    assert retried["job_id"] == resumed_id and retried["created"] is False
    daemon._admit()
    attempt = daemon.store.list_attempts(resumed_id)[0]
    launches = []

    class ObserveResume(FakeAdapter):
        def resume_launch(self, *args, **kwargs):
            launches.append(adapter.resume_launch(*args, **kwargs))
            raise AdapterError("stopped after constructing native launch")

    monkeypatch.setattr(module, "get_adapter", lambda _: ObserveResume())
    Daemon._launch(daemon, attempt)
    assert len(launches) == 1
    resumed = launches[0]
    for launch in (fresh, resumed):
        argv = launch.argv
        if effort is None:
            assert "--effort" not in argv
        else:
            assert argv[argv.index("--effort") + 1] == effort
        assert argv.count("--settings") == 1
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["env"].get("CLAUDE_CODE_EFFORT_LEVEL") == effort
    assert daemon.store.get_job(resumed_id)["pinned_model"] == "opus_alternate"
    assert_resume_pins_source_model_id(daemon, resumed_id)
    assert resumed.native_session_id == "native-source-session"
    assert daemon._children == {}


# Review r3/r4 of #310 (D-EFFORT-ENV1): keep a recorded alias that still names an entry
# at the source model ID. Otherwise, select a current entry by its ID.

#: A synthetic successor ID for `haiku`, so its old ID is left to another alias or to none.
NEXT_HAIKU_ID = "claude-haiku-9-9-synthetic"
#: The fix of a resume refused for its source's model (C-12.4, C-25.7).
RESUME_MODEL_FIX = "submit a fresh job: a resume always runs on its source's model"


def assert_resume_pins_source_model_id(daemon, job_id):
    """The round's invariant: an accepted resume pins a policy entry whose `id` is its
    source attempt's provider model ID, the identity its launch checks."""
    pinned = daemon.store.get_job(job_id)["pinned_model"]
    manifest = json.loads((daemon.root / "jobs" / job_id / "manifest.json").read_text())
    assert daemon.policy["models"][pinned]["id"] == manifest["resume"]["model_id"]


def launch_resume(daemon, monkeypatch, job_id):
    """Admit a resume and run `_launch` to the adapter's native continuation. What that
    call received, or [] when `_launch` refused before it (a `spawn_error`)."""
    daemon._admit()
    attempt = daemon.store.list_attempts(job_id)[0]
    calls = []

    class ObserveResume(FakeAdapter):
        def resume_launch(self, spec, aid, adir, lane, env, native, prompt, guard, model_id=None, effort=None):
            calls.append((lane.lane_id, native, model_id, effort))
            raise AdapterError("stopped after observing native launch")

        def build_launch(self, *args):
            raise AssertionError("a resume must not start a fresh provider session")

    with monkeypatch.context() as patch:
        patch.setattr(module, "get_adapter", lambda _: ObserveResume())
        Daemon._launch(daemon, attempt)
    assert daemon._children == {}
    return calls


def claude_lane(daemon):
    lane = daemon.store.get_lane("codex-1")
    daemon.store.put_lane(replace(lane, lane_id="claude-1", provider="claude",
        account_key="claude:fixture", credential=Credential("claude", lane.home, "home")))
    daemon.desktop_prober = lambda: None
    register("claude", FakeAdapter)


def reload_policy(daemon):
    """The daemon's policy as edited in memory, written and read back as the daemon reads it."""
    policy_path = daemon.root / "policy.json"
    policy_path.write_text(json.dumps(daemon.policy))
    daemon.policy = load_policy(policy_path)


@pytest.mark.parametrize("model_short", ["Astra", "astra-v1-only-name", None, "astra", "gpt-6-astra"])
def test_c23_32_imported_resume_runs_at_its_source_model_id_whatever_v1_named_it(
        state_daemon, monkeypatch, model_short):
    """C-12.4, C-23.32 (review r3 of #310, P1): the importer records v1's own name for the
    requested model, which it resolved without case and by scope. A name that is no current
    key, retired alias or ID resumes as before the alias was kept: on the source's lane, at
    its provider model ID. `None`, the alias and the ID are controls. The imported row is
    read, never rewritten."""
    daemon, harness = state_daemon
    source_id, attempt = finished_source(daemon, harness)
    evidence = json.dumps({"imported": True, "imported_external": False, "model_short": model_short,
                           "v1_class": None, "v1_lane": "codex-a",
                           "v1_meta": "/legacy/runs/20260905-111855-astra/meta.json", "salvage_refs": []},
                          sort_keys=True)
    daemon.store.update_attempt(attempt["attempt_id"], evidence_json=evidence)
    source_attempt = daemon.store.get_attempt(attempt["attempt_id"])
    assert source_attempt["model_requested"] == "gpt-6-astra"
    resumed_id = resume_job(daemon, harness, source_id)
    assert daemon.store.get_attempt(attempt["attempt_id"]) == source_attempt
    resumed = daemon.store.get_job(resumed_id)
    assert (resumed["pinned_lane"], resumed["pinned_model"]) == (source_attempt["lane_id"], "astra")
    assert_resume_pins_source_model_id(daemon, resumed_id)
    assert launch_resume(daemon, monkeypatch, resumed_id) == [
        (source_attempt["lane_id"], "native-source-session", "gpt-6-astra",
         daemon.policy["models"]["astra"].get("effort"))]
    assert daemon.store.get_attempt(attempt["attempt_id"]) == source_attempt
    assert source_attempt["evidence_json"] == evidence


def test_imported_resume_pins_source_id_when_a_current_key_shadows_it(state_daemon, monkeypatch):
    """C-12.4, C-23.32: a current key spelled like the source ID can point to a different model."""
    daemon, harness = state_daemon
    source_id, attempt = finished_source(daemon, harness)
    evidence = json.dumps({"imported": True, "imported_external": False,
                           "model_short": "astra-v1-only-name", "v1_class": None, "v1_lane": "codex-a",
                           "v1_meta": "/legacy/runs/20260905-111855-astra/meta.json", "salvage_refs": []},
                          sort_keys=True)
    daemon.store.update_attempt(attempt["attempt_id"], evidence_json=evidence)
    source_attempt = daemon.store.get_attempt(attempt["attempt_id"])
    old_id = source_attempt["model_requested"]
    models = daemon.policy["models"]
    models[old_id] = {**models["astra"], "id": "gpt-6-astra-synthetic", "effort": "low"}
    reload_policy(daemon)

    resumed_id = resume_job(daemon, harness, source_id)
    assert daemon.store.get_attempt(attempt["attempt_id"]) == source_attempt
    launched = launch_resume(daemon, monkeypatch, resumed_id)
    assert (daemon.store.get_job(resumed_id)["pinned_model"], launched) == ("astra", [
        (source_attempt["lane_id"], "native-source-session", old_id,
         daemon.policy["models"]["astra"].get("effort"))])
    assert_resume_pins_source_model_id(daemon, resumed_id)
    assert daemon.store.get_attempt(attempt["attempt_id"]) == source_attempt


@pytest.mark.parametrize("change", ["id-rollover", "alias-removed", "alias-retired", "id-rollover-retired-shadow"])
def test_resume_after_a_policy_change_pins_an_entry_at_its_source_model_id(state_daemon, monkeypatch, change):
    """C-12.4: after an ID rollover or alias removal, the first current entry at the
    source ID supplies the launch effort, even when a retired alias shadows that ID.
    The recorded alias can itself be retired while still resolving to the source ID."""
    daemon, harness = state_daemon
    models = daemon.policy["models"]
    models["opus"]["effort"] = "low"
    models["opus_alternate"] = {**models["opus"], "effort": "high"}
    reload_policy(daemon)
    claude_lane(daemon)
    rollover = change.startswith("id-rollover")
    source_model = "haiku" if rollover else "opus_alternate"
    source_id, source_attempt = finished_source(daemon, harness, pinned_lane="claude-1", pinned_model=source_model)
    old_id = source_attempt["model_requested"]
    assert json.loads(source_attempt["evidence_json"])["model_short"] == source_model
    models = daemon.policy["models"]
    if rollover:
        models["haiku_legacy"] = {**models["haiku"]}
        models["haiku"] = {**models["haiku"], "id": NEXT_HAIKU_ID, "effort": "high"}
        if change == "id-rollover-retired-shadow":
            daemon.policy["retired"][old_id] = "haiku"
            models["haiku_legacy"]["effort"] = "medium"
            models["haiku_alternate"] = {**models["haiku_legacy"], "effort": "low"}
        expected = "haiku_legacy"
    else:
        del models["opus_alternate"]
        if change == "alias-retired":
            daemon.policy.setdefault("retired", {})["opus_alternate"] = "opus"
        expected = "opus"
    reload_policy(daemon)
    resumed_id = resume_job(daemon, harness, source_id)
    # The launch first: before this round an ID rollover was accepted, then refused here.
    launched = launch_resume(daemon, monkeypatch, resumed_id)
    assert (daemon.store.get_job(resumed_id)["pinned_model"], launched) == (expected, [
        ("claude-1", "native-source-session", old_id, daemon.policy["models"][expected].get("effort"))])
    assert_resume_pins_source_model_id(daemon, resumed_id)


@pytest.mark.parametrize("retired_shadow", [False, True], ids=["no-alias", "retired-shadow"])
def test_resume_is_refused_unknown_pinned_model_when_no_entry_holds_its_source_model_id(
        state_daemon, retired_shadow):
    """C-12.4, C-25.7 (review r3 of #310, P2 and P3): when no policy entry holds the source
    attempt's model ID, the resume is refused at its pin, before the request digest, as
    `unknown-pinned-model` (code 7) naming no policy path. The caller cannot change a resume's
    pin, so the repair is a fresh job."""
    daemon, harness = state_daemon
    claude_lane(daemon)
    source_id, source_attempt = finished_source(daemon, harness, pinned_lane="claude-1", pinned_model="haiku")
    old_id = source_attempt["model_requested"]
    daemon.policy["models"]["haiku"]["id"] = NEXT_HAIKU_ID
    if retired_shadow:
        daemon.policy["retired"][old_id] = "haiku"
    reload_policy(daemon)
    with pytest.raises(AdapterError) as raised:
        resume_job(daemon, harness, source_id)
    refusal = raised.value
    assert type(refusal) is AdapterError
    assert (refusal.reason, refusal.code) == ("unknown-pinned-model", 7)
    assert str(refusal) == (f"unknown pinned model {old_id!r}; policy models: "
                            + ", ".join(daemon.policy["models"]))
    assert "policy.json" not in str(refusal) and "pinned_model" not in str(refusal)
    assert refusal.details == {"model": old_id, "resume_pin": True}
    assert refusal.fix == RESUME_MODEL_FIX
    assert protocol.refusal_text(refusal) == (f"unknown pinned model {old_id}", RESUME_MODEL_FIX)
    assert [job["job_id"] for job in daemon.store.list_jobs()] == [source_id]


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
        def resume_launch(self, spec, aid, adir, lane, env, native, prompt, guard, model_id=None, effort=None):
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


def test_d635_resume_fence_is_released_with_the_job_insert(state_daemon):
    """d635 (review of a9a6cbf4): the resume's `retire:` fence on its source is
    released in the transaction that inserts the resume's job, so the source is
    then pinned only as a parent (C-8.4), not by a fence left behind."""
    daemon, harness = state_daemon
    source_id, _ = finished_source(daemon, harness)
    resumed_id = daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume",
        parent_job_id=source_id)))["job_id"]
    assert daemon.store.get_job(resumed_id)["parent_job_id"] == source_id
    assert not daemon.store.query("SELECT * FROM leases WHERE lease_key LIKE 'retire:%'")


def test_d635_resume_is_refused_while_retention_retires_the_source(state_daemon):
    """Astra design finding 9: a resume never reads a source retention is moving."""
    daemon, harness = state_daemon
    source_id, _ = finished_source(daemon, harness)
    assert daemon.store.acquire_lease(f"retire:{source_id}", f"retention:{source_id}")
    with pytest.raises(AdapterError, match="being archived by retention"):
        daemon.submit(protocol.SubmitArgs(**harness.submit_args(kind="resume", parent_job_id=source_id)))
    assert not daemon.store.query("SELECT * FROM jobs WHERE parent_job_id=?", (source_id,))
