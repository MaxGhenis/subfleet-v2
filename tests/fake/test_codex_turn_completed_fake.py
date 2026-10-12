"""D-SHUT1 acceptance, export, retry and replay with the real Codex adapter."""

from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from subfleet import daemon as module
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
        assert "Codex exited before its turn completed" not in daemon.dispatch("why", {"job_id": job_id})["text"]
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
            assert row["outcome_class"] == "transient"
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
