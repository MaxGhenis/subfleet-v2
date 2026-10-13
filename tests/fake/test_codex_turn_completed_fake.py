"""D-SHUT1 acceptance, export, retry and replay with the real Codex adapter."""

from argparse import Namespace
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from subfleet import cli, daemon as module
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import ExitInfo, OutcomeClass
from subfleet.daemon import Daemon
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"
CUT_OFF_CASES = ("cut-off-item-completed", "cut-off-item-started", "cut-off-error")


def stage_fixture(daemon, adapter, attempt, adir, case, *, rc=0):
    spec = daemon._spec(daemon.store.get_job(attempt["job_id"]))
    launch = adapter.build_launch(spec, attempt["attempt_id"], adir,
                                  daemon.store.get_lane(attempt["lane_id"]), {},
                                  attempt["model_requested"], None, Path(spec.prompt_path), None)
    saved = asdict(launch)
    saved.pop("env_add")
    (adir / "launch.json").write_text(json.dumps(saved))
    fixture = FIXTURES / case
    finalizing = receipt_fixture(daemon, attempt, adir, rc=rc, stdout=(fixture / "stdout").read_bytes())
    (adir / "stderr").write_bytes((fixture / "stderr").read_bytes())
    return launch, finalizing


def finalize_fixture(daemon, adapter, attempt, adir, case):
    _, finalizing = stage_fixture(daemon, adapter, attempt, adir, case)
    daemon._finalize(finalizing)


@pytest.mark.parametrize("case", CUT_OFF_CASES)
def test_cut_off_attempt_is_never_accepted_or_exported(state_daemon, monkeypatch, case):
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous output\n")
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1, out_path=str(out))
    adapter = CodexAdapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    finalize_fixture(daemon, adapter, attempt, adir, case)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["accepted_attempt_id"]) == ("failed", None)
    row = daemon.store.get_attempt(attempt["attempt_id"])
    assert (row["outcome_class"], row["rc"], row["killed_by"]) == ("transient", 0, None)
    evidence = json.loads(row["evidence_json"])["classification"]
    assert evidence["turn_completed"] is False
    assert evidence["last_event_type"] == {"cut-off-item-completed": "item.completed",
                                           "cut-off-item-started": "item.started",
                                           "cut-off-error": "error"}[case]
    notice, = daemon.store.list_notices()
    assert "Codex exited before its turn completed" in notice["text"]
    why = daemon.dispatch("why", {"job_id": job_id})
    assert "Codex exited before its turn completed" in why["text"]
    assert f"last event: {evidence['last_event_type']}" in why["text"]
    assert why["attempt_explanation"] == (
        "Last attempt a1: Codex exited before its turn completed "
        f"(last event: {evidence['last_event_type']})")
    # Force the export path too: it must refuse a job with no accepted attempt.
    daemon._export(job_id)
    assert out.read_bytes() == b"previous output\n"
    assert daemon.store.get_job(job_id)["export_error"] == "export failed: no accepted attempt"
    assert not daemon.store.query("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'",
                                  (attempt["attempt_id"],))


@pytest.mark.parametrize("second_case,expected", [("success", "succeeded"),
                                                   ("cut-off-item-started", "failed")])
def test_retry_uses_only_the_next_attempts_deliverable(state_daemon, monkeypatch, second_case, expected):
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_bytes(b"previous output\n")
    job_id, first, first_dir = reserve(daemon, harness, max_attempts=2, out_path=str(out))
    adapter = CodexAdapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    finalize_fixture(daemon, adapter, first, first_dir, "cut-off-item-completed")
    waiting = daemon.store.get_job(job_id)
    assert (waiting["state"], waiting["accepted_attempt_id"]) == ("waiting", None)
    assert waiting["next_check_at"] is not None
    assert "Codex exited before its turn completed" in daemon.dispatch("why", {"job_id": job_id})["text"]
    assert out.read_bytes() == b"previous output\n"
    assert b"before finishing" in (first_dir / "deliverable.md").read_bytes()
    daemon.store.update_job(job_id, next_check_at="2000-01-01T00:00:00Z")
    daemon._admit()
    second = daemon.store.list_attempts(job_id)[-1]
    assert second["seq"] == 2
    daemon._pending_launches.discard(second["attempt_id"])
    second_dir = daemon.root / "jobs" / job_id / "a2"
    second_dir.mkdir(mode=0o700)
    finalize_fixture(daemon, adapter, second, second_dir, second_case)
    job = daemon.store.get_job(job_id)
    assert job["state"] == expected
    if expected == "succeeded":
        assert job["accepted_attempt_id"] == second["attempt_id"]
        assert out.read_bytes() == (FIXTURES / "success" / "last.md").read_bytes()
        assert out.read_bytes() == (second_dir / "deliverable.md").read_bytes()
        why = daemon.dispatch("why", {"job_id": job_id})
        assert "Codex exited before its turn completed" not in why["text"]
        assert why["attempt_explanation"] is None
    else:
        assert job["accepted_attempt_id"] is None
        assert (second_dir / "deliverable.md").read_bytes() == b""
        assert out.read_bytes() == b"previous output\n"


def test_cut_off_verdict_survives_finalization_replay(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1)
    adapter = CodexAdapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    notice = daemon._notice

    def crash(*args):
        raise RuntimeError("interrupted before acceptance")

    monkeypatch.setattr(daemon, "_notice", crash)
    with pytest.raises(RuntimeError, match="interrupted before acceptance"):
        finalize_fixture(daemon, adapter, attempt, adir, "cut-off-item-completed")
    frozen = json.loads((adir / "finalization.json").read_text())["outcome"]
    assert frozen["cls"] == "transient" and frozen["evidence"]["turn_completed"] is False
    monkeypatch.setattr(daemon, "_notice", notice)
    daemon._finalize(daemon.store.get_attempt(attempt["attempt_id"]))
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["accepted_attempt_id"]) == ("failed", None)
    assert daemon.store.get_attempt(attempt["attempt_id"])["outcome_class"] == "transient"


@pytest.mark.parametrize("source", ("error-event", "stderr"))
@pytest.mark.parametrize("path", ("fresh", "cached", "recovery"))
def test_completed_success_survives_premerge_auth_receipt(state_daemon, monkeypatch, source, path):
    """Review r2 P1: reproduce 4658ea6d2's auth verdict on a completed run."""
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    job_id, attempt, adir = reserve(daemon, harness, out_path=str(out))
    adapter = CodexAdapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    launch, finalizing = stage_fixture(daemon, adapter, attempt, adir, "success")
    diagnostic = "Your refresh token was revoked."
    error = {"type": "error", "message": diagnostic}
    events = ([error] if source == "error-event" else []) + [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Finished."}},
        {"type": "turn.completed"},
    ]
    (adir / "stdout").write_text("".join(json.dumps(event) + "\n" for event in events))
    (adir / "stderr").write_text(diagnostic if source == "stderr" else "")
    fresh = adapter.classify(adir, launch, ExitInfo(0, None, .1, None, None))
    assert fresh.cls == OutcomeClass.OK
    observed = []
    answered = Daemon._answered
    def observe(outcome, provider_class):
        observed.append((outcome.cls.value, provider_class))
        return answered(outcome, provider_class)
    monkeypatch.setattr(Daemon, "_answered", staticmethod(observe))
    frozen = None
    if path != "fresh":
        # These are the exact fields the pre-merge classifier recorded: it
        # checked authentication before the same verified-success evidence.
        legacy = replace(fresh, cls=OutcomeClass.AUTH_DEAD, detail="Subscription authentication rejected",
                         evidence={**fresh.evidence, "authentication": error if source == "error-event" else diagnostic,
                                   "admission": None})
        attest = adapter.attest(adir, launch, legacy, attempt["model_requested"])
        frozen = json.dumps({"outcome": asdict(legacy), "attestation": asdict(attest)})
        (adir / "finalization.json").write_text(frozen)
        monkeypatch.setattr(adapter, "classify", lambda *args: pytest.fail("cached classification repeated"))
        salvage = daemon._salvage
        def crash(*args):
            raise RuntimeError("interrupted before acceptance")
        monkeypatch.setattr(daemon, "_salvage", crash)
        with pytest.raises(RuntimeError, match="interrupted before acceptance"):
            daemon._finalize(finalizing)
        assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "finalizing"
        assert (adir / "deliverable.md").read_bytes() == b"Finished."
        assert (adir / "stream.jsonl").read_bytes() == (adir / "stdout").read_bytes()
        (adir / "stdout").write_text('{"type":"turn.failed"}\n')
        (adir / "last.md").write_bytes(b" \n")
        monkeypatch.setattr(daemon, "_salvage", salvage)
    successor = None
    try:
        if path == "recovery":
            daemon.close()
            successor = daemon = Daemon(harness.root)
            monkeypatch.setattr(daemon, "_launch", lambda *args: pytest.fail("recovery must not launch"))
            daemon._process_attempt(attempt["attempt_id"])
        else:
            daemon._finalize(finalizing)
        job = daemon.store.get_job(job_id)
        assert (job["state"], job["accepted_attempt_id"]) == ("succeeded", attempt["attempt_id"])
        row = daemon.store.get_attempt(attempt["attempt_id"])
        assert (row["outcome_class"], row["rc"], row["killed_by"]) == ("ok", 0, None)
        assert out.read_bytes() == b"Finished."
        assert daemon.store.get_lane(attempt["lane_id"]).enabled
        assert daemon.store.list_closures() == []
        evidence = json.loads(row["evidence_json"])
        assert evidence["classification"]["turn_completed"] is True
        assert evidence["classification"]["last_event_type"] == "turn.completed"
        if frozen is not None:
            assert (adir / "finalization.json").read_text() == frozen
            assert evidence["provider_verdict"] == {
                "class": "auth-dead", "detail": "Subscription authentication rejected", "killed_by": None}
        assert observed == ([("ok", "ok")] if path == "fresh" else [("ok", "auth-dead")] * 2)
        daemon._finalize(row)  # Terminal attempts stay untouched; no second notice/export.
        assert daemon.store.get_attempt(attempt["attempt_id"]) == row
        assert len(daemon.store.list_notices()) == 1
    finally:
        if successor is not None:
            successor.close()


@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
def test_empty_frozen_stream_cannot_accept_later_stdout_completion(state_daemon, monkeypatch, restart):
    """Review r3 P1: only live stdout changes after an empty stream is frozen."""
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1, out_path=str(out))
    adapter = CodexAdapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    launch, finalizing = stage_fixture(daemon, adapter, attempt, adir, "success")
    interim = b"Interim progress, not a finished turn."
    (adir / "stdout").write_bytes(b"")
    (adir / "stderr").write_text("Your refresh token was revoked.")
    (adir / "last.md").write_bytes(interim)
    # With no stream events, this matches the pre-merge classifier's auth-dead
    # verdict; the completion/success precedence change cannot affect it.
    verdict = adapter.classify(adir, launch, ExitInfo(0, None, .1, None, None))
    assert verdict.cls == OutcomeClass.AUTH_DEAD
    attest = adapter.attest(adir, launch, verdict, attempt["model_requested"])
    (adir / "finalization.json").write_text(json.dumps({"outcome": asdict(verdict),
                                                       "attestation": asdict(attest)}))
    monkeypatch.setattr(adapter, "classify", lambda *args: pytest.fail("cached classification repeated"))
    monkeypatch.setattr(adapter, "attest", lambda *args: pytest.fail("cached attestation repeated"))
    observed = []
    answered = Daemon._answered

    def observe(outcome, provider_class):
        observed.append(outcome.cls.value)
        return answered(outcome, provider_class)

    monkeypatch.setattr(Daemon, "_answered", staticmethod(observe))
    salvage = daemon._salvage

    def crash(*args):
        raise RuntimeError("interrupted before acceptance")

    monkeypatch.setattr(daemon, "_salvage", crash)
    with pytest.raises(RuntimeError, match="interrupted before acceptance"):
        daemon._finalize(finalizing)
    assert observed == ["auth-dead"]
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "finalizing"
    assert daemon.store.get_job(job_id)["accepted_attempt_id"] is None
    assert (adir / "stream.jsonl").read_bytes() == b""
    assert (adir / "deliverable.md").read_bytes() == interim
    frozen = {name: (adir / name).read_bytes() for name in
              ("stream.jsonl", "deliverable.md", "finalization.json", "exit.json", "last.md", "stderr")}
    monkeypatch.setattr(daemon, "_salvage", salvage)
    monkeypatch.setattr(adapter, "deliverable", lambda *args: pytest.fail("captured deliverable extracted again"))
    with (adir / "stdout").open("a") as stream:
        stream.write('{"type":"turn.completed"}\n')
    successor = None
    try:
        if restart:
            daemon.close()
            successor = daemon = Daemon(harness.root)
            monkeypatch.setattr(daemon, "_launch", lambda *args: pytest.fail("recovery must not launch"))
            daemon._process_attempt(attempt["attempt_id"])
        else:
            daemon._finalize(finalizing)
        assert {name: (adir / name).read_bytes() for name in frozen} == frozen
        assert observed == ["auth-dead", "auth-dead"]
        job = daemon.store.get_job(job_id)
        # C-4.5: auth rejection disables the lane without counting the attempt.
        assert (job["state"], job["accepted_attempt_id"]) == ("waiting", None)
        assert not daemon.store.get_lane(attempt["lane_id"]).enabled
        row = daemon.store.get_attempt(attempt["attempt_id"])
        assert (row["outcome_class"], row["rc"], row["killed_by"]) == ("auth-dead", 0, None)
        evidence = json.loads(row["evidence_json"])["classification"]
        assert evidence["turn_completed"] is False
        assert evidence["turn_failed"] is False
        assert evidence["last_event_type"] is None
        assert not out.exists()
        assert not daemon.store.query("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'",
                                      (attempt["attempt_id"],))
    finally:
        if successor is not None:
            successor.close()


@pytest.mark.parametrize("guard", ("turn-failed", "spawn-error", "cached-spawn-error", "killed",
                                  "nonzero", "blank", "incomplete"))
def test_cached_auth_reconciliation_requires_all_success_evidence(state_daemon, monkeypatch, guard):
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_text("previous output\n")
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1, out_path=str(out))
    adapter = CodexAdapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    rc = 1 if guard == "nonzero" else 0
    launch, finalizing = stage_fixture(daemon, adapter, attempt, adir, "success", rc=rc)
    error = {"type": "error", "message": "Your refresh token was revoked."}
    events = [error, {"type": "item.completed", "item": {"type": "agent_message", "text": "Finished."}}]
    if guard == "turn-failed":
        events.append({"type": "turn.failed", "error": {"message": "Terminal provider failure"}})
    if guard != "incomplete":
        events.append({"type": "turn.completed"})
    (adir / "stdout").write_text("".join(json.dumps(event) + "\n" for event in events))
    verdict = adapter.classify(adir, launch, ExitInfo(rc, None, .1, None, None))
    verdict = replace(verdict, cls=OutcomeClass.AUTH_DEAD, detail="Subscription authentication rejected",
                      evidence={**verdict.evidence, "authentication": error, "admission": None,
                                "turn_completed": True})  # A cached claim cannot supply missing proof.
    if guard == "spawn-error":
        receipt = json.loads((adir / "exit.json").read_text())
        receipt["spawn_error"] = "Provider could not spawn"
        (adir / "exit.json").write_text(json.dumps(receipt))
    elif guard == "cached-spawn-error":
        verdict = replace(verdict, evidence={**verdict.evidence, "spawn_error": "Provider could not spawn"})
    elif guard == "killed":
        daemon.store.update_attempt(attempt["attempt_id"], killed_by="operator")
    elif guard == "blank":
        # The stream has nonblank agent text, but replay must use these already
        # captured blank bytes rather than extracting the stream again.
        (adir / "deliverable.md").write_bytes(b" \n\t")
    attest = adapter.attest(adir, launch, verdict, attempt["model_requested"])
    frozen = json.dumps({"outcome": asdict(verdict), "attestation": asdict(attest)})
    (adir / "finalization.json").write_text(frozen)
    monkeypatch.setattr(adapter, "classify", lambda *args: pytest.fail("cached classification repeated"))
    daemon._finalize(finalizing)
    job = daemon.store.get_job(job_id)
    assert job["state"] != "succeeded"
    assert job["accepted_attempt_id"] is None
    assert daemon.store.get_attempt(attempt["attempt_id"])["outcome_class"] != "ok"
    assert out.read_text() == "previous output\n"
    assert (adir / "finalization.json").read_text() == frozen


@pytest.mark.parametrize("verdict", (OutcomeClass.TRANSIENT, OutcomeClass.AUTH_DEAD, OutcomeClass.UNKNOWN))
@pytest.mark.parametrize("recorder", ("this-daemon", "older-daemon"))
@pytest.mark.parametrize("restart", (False, True), ids=("replay", "restart"))
def test_replay_keeps_this_daemons_verdict_and_reconciles_only_an_older_one(
        state_daemon, monkeypatch, verdict, recorder, restart):
    """CI at 8fb1a8239: a retry whose salvage timed out (C-13.1) is finalized again,
    and reconciling its own cached `transient` with the completed stream made it
    succeed. A verdict this daemon classified is now replayed as its first pass
    decided it (C-4.3); only one an older daemon recorded, without `rule`, is
    reconciled with the frozen completion proof (C-9.2)."""
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_text("previous output\n")
    job_id, attempt, adir = reserve(daemon, harness, out_path=str(out))
    adapter = CodexAdapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    launch, finalizing = stage_fixture(daemon, adapter, attempt, adir, "success")
    proven = adapter.classify(adir, launch, ExitInfo(0, None, .1, None, None))
    assert proven.cls == OutcomeClass.OK  # The stream proves verified success.
    monkeypatch.setattr(adapter, "classify", lambda *args: replace(proven, cls=verdict, detail="fixture verdict"))
    observed = []
    answered = Daemon._answered

    def observe(outcome, provider_class):
        observed.append(outcome.cls.value)
        return answered(outcome, provider_class)

    monkeypatch.setattr(Daemon, "_answered", staticmethod(observe))
    salvage = daemon._salvage

    def crash(*args):
        raise RuntimeError("interrupted before acceptance")

    monkeypatch.setattr(daemon, "_salvage", crash)
    with pytest.raises(RuntimeError, match="interrupted before acceptance"):
        daemon._finalize(finalizing)
    frozen = json.loads((adir / "finalization.json").read_text())
    assert (frozen["rule"], frozen["outcome"]["cls"]) == (module.FINALIZATION_RULE, verdict.value)
    if recorder == "older-daemon":
        del frozen["rule"]
        (adir / "finalization.json").write_text(json.dumps(frozen))
    monkeypatch.setattr(daemon, "_salvage", salvage)
    monkeypatch.setattr(adapter, "classify", lambda *args: pytest.fail("cached classification repeated"))
    successor = None
    try:
        if restart:
            daemon.close()
            successor = daemon = Daemon(harness.root)
            monkeypatch.setattr(daemon, "_launch", lambda *args: pytest.fail("recovery must not launch"))
            daemon._process_attempt(attempt["attempt_id"])
        else:
            daemon._finalize(finalizing)
        row = daemon.store.get_attempt(attempt["attempt_id"])
        job = daemon.store.get_job(job_id)
        evidence = json.loads(row["evidence_json"])
        if recorder == "this-daemon":
            assert observed == [verdict.value] * 2
            assert row["outcome_class"] == verdict.value
            assert job["state"] != "succeeded" and job["accepted_attempt_id"] is None
            assert "provider_verdict" not in evidence
            assert out.read_text() == "previous output\n"
        else:
            assert observed == [verdict.value, "ok"]
            assert (row["outcome_class"], job["state"]) == ("ok", "succeeded")
            assert job["accepted_attempt_id"] == attempt["attempt_id"]
            assert evidence["provider_verdict"] == {"class": verdict.value, "detail": "fixture verdict",
                                                    "killed_by": None}
            assert out.read_bytes() == (adir / "deliverable.md").read_bytes()
        assert evidence["classification"]["turn_completed"] is True
    finally:
        if successor is not None:
            successor.close()


@pytest.mark.parametrize("path", ("fresh", "cached", "legacy", "recovery"))
@pytest.mark.parametrize("complete", (False, True))
@pytest.mark.parametrize("rc", (0, 1))
@pytest.mark.parametrize("last", (None, "interim", "final"))
def test_i1_succeeded_requires_completion_on_every_finalization_path(
        state_daemon, monkeypatch, path, complete, rc, last):
    """Exhaustive 48-case daemon invariant, including pre-upgrade receipts."""
    daemon, harness = state_daemon
    out = harness.root / "out.md"
    out.write_text("previous output\n")
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1, out_path=str(out))
    adapter = CodexAdapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    launch, finalizing = stage_fixture(daemon, adapter, attempt, adir, "cut-off-item-completed", rc=rc)
    interim = json.loads((adir / "stdout").read_text().splitlines()[-1])["item"]["text"]
    if complete:
        with (adir / "stdout").open("a") as stream:
            stream.write('{"type":"turn.completed"}\n')
    if last is not None:
        (adir / "last.md").write_text(interim if last == "interim" else "Final result.")
    if path != "fresh":
        outcome = adapter.classify(adir, launch, ExitInfo(rc, None, .1, None, None))
        # Replay untrusted `ok`, even a cached claim of completion. Only the
        # attempt stream can prove it; the legacy schema lacks those fields.
        evidence = {**outcome.evidence, "turn_completed": True, "last_event_type": "turn.completed"}
        if path in ("legacy", "recovery"):
            evidence.pop("turn_completed")
            evidence.pop("last_event_type")
        verdict = replace(outcome, cls=OutcomeClass.OK, detail="legacy provider success", evidence=evidence)
        attest = adapter.attest(adir, launch, verdict, attempt["model_requested"])
        (adir / "finalization.json").write_text(json.dumps({"outcome": asdict(verdict),
                                                           "attestation": asdict(attest)}))
        def refuse_reclassification(*args):
            raise AssertionError("cached provider classification was repeated")
        monkeypatch.setattr(adapter, "classify", refuse_reclassification)
    successor = None
    try:
        if path == "recovery":
            daemon.close()
            successor = daemon = Daemon(harness.root)
            monkeypatch.setattr(daemon, "_launch", lambda *args: pytest.fail("recovery must not launch"))
            daemon._process_attempt(attempt["attempt_id"])
        else:
            daemon._finalize(finalizing)
        job = daemon.store.get_job(job_id)
        accepted = complete and rc == 0
        assert (job["state"] == "succeeded") == accepted
        assert job["accepted_attempt_id"] == (attempt["attempt_id"] if accepted else None)
        row = daemon.store.get_attempt(attempt["attempt_id"])
        evidence = json.loads(row["evidence_json"])
        if not complete:
            # Fresh nonzero exits keep the adapter's existing classification.
            # Cached `ok` verdicts are overridden when completion is absent.
            expected_class = "unknown" if path == "fresh" and rc != 0 else "transient"
            assert row["outcome_class"] == expected_class
            assert evidence["classification"]["turn_completed"] is False
            assert evidence["classification"]["last_event_type"] == "item.completed"
            if path != "fresh":
                assert evidence["provider_verdict"] == {
                    "class": "ok", "detail": "legacy provider success", "killed_by": None}
        assert out.read_text() == ((adir / "deliverable.md").read_text() if accepted else "previous output\n")
        assert len(daemon.store.list_notices()) == 1
    finally:
        if successor is not None:
            successor.close()


@pytest.mark.parametrize("explicit_out", (False, True))
def test_runs_show_separates_failed_interim_evidence(state_daemon, monkeypatch, capsys, explicit_out):
    """Port the review's corrected fixture text assertion as desired behavior."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1)
    adapter = CodexAdapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    finalize_fixture(daemon, adapter, attempt, adir, "cut-off-item-completed")
    interim = json.loads((adir / "stdout").read_text().splitlines()[-1])["item"]["text"]
    class Client:
        def call(self, operation, args):
            return daemon.dispatch(operation, args)
    monkeypatch.setattr(cli, "_client", lambda _: Client())
    monkeypatch.setattr(cli, "_ack_notices", lambda *args: None)
    assert cli.cmd_runs_show(Namespace(id=job_id, json=False, out=explicit_out, err=False)) == 1
    captured = capsys.readouterr()
    assert interim not in captured.out
    if explicit_out:
        assert captured.out == ""
        assert "no deliverable recorded" in captured.err
    else:
        # C-17.1: v1's shape, the metadata object and `--- out.md ---`, with
        # nothing after it; the unaccepted evidence is named on stderr.
        metadata, result = captured.out.split("\n--- out.md ---\n")
        assert json.loads(metadata)["job"]["accepted_attempt_id"] is None
        assert result == ""
        assert "No accepted result" in captured.err
        assert f"Unaccepted evidence ({attempt['attempt_id']}): {adir / 'deliverable.md'}" in captured.err


@pytest.mark.parametrize("legacy", (False, True))
def test_restart_in_transient_backoff_preserves_deadline(state_daemon, monkeypatch, legacy):
    daemon, harness = state_daemon
    class Clock(datetime):
        current = datetime.now(timezone.utc)
        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)
    monkeypatch.setattr(module, "datetime", Clock)
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=2)
    adapter = CodexAdapter()
    monkeypatch.setattr(module, "get_adapter", lambda _: adapter)
    finalize_fixture(daemon, adapter, attempt, adir, "cut-off-item-completed")
    deadline = daemon.store.get_job(job_id)["next_check_at"]
    assert deadline == module.after(60)
    if legacy:
        # Existing waiting rows have no completion or new retry metadata.
        daemon.store.update_attempt(attempt["attempt_id"], evidence_json="{}")
    Clock.current += timedelta(seconds=30)
    daemon.close()
    successor = Daemon(harness.root)
    monkeypatch.setattr(successor, "_launch", lambda *args: pytest.fail("state-only test"))
    try:
        successor._recover_capacity_waits()
        assert successor.store.get_job(job_id)["next_check_at"] == deadline
        successor._admit()
        assert len(successor.store.list_attempts(job_id)) == 1
        Clock.current += timedelta(seconds=29)
        successor._admit()
        assert len(successor.store.list_attempts(job_id)) == 1
        Clock.current += timedelta(seconds=1)
        successor._admit()
        second = successor.store.list_attempts(job_id)[-1]
        assert second["seq"] == 2
        successor._pending_launches.discard(second["attempt_id"])
    finally:
        successor.close()


@pytest.mark.parametrize("reason", ("capacity", "route"))
def test_recovery_still_rechecks_ordinary_waits(state_daemon, reason):
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon.store.update_job(job_id, state="waiting", wait_reason=reason, next_check_at=module.after(600))
    daemon._recover_capacity_waits()
    assert daemon.store.get_job(job_id)["next_check_at"] <= module.utcnow()
