"""A notice names the job's terminal state, and a signalled attempt is never `ok`.

C-15.1 and C-9.2, from the incident of 2026-09-24: three running Codex jobs were
cancelled; each attempt exited 0 after the daemon's SIGTERM with an interim
progress message in `last.md`, the adapter classified it `ok`, and the notice
was written from the attempt (`ok; rc=0; deliverable=...; out=...`) while the
job was `cancelled` with rc 130 and nothing had been exported.

Every test here drives the daemon in process with no provider: each call site
that writes a notice is reached through the daemon's own code path, and the
header is read back from the store. Process identity is stubbed exactly as the
`state_daemon` fixture stubs it; where a signal would be sent, the stub stands
in for the provider's reaction to it and says so.
"""

from __future__ import annotations

import json
from pathlib import Path
import signal

import pytest

from subfleet import daemon as daemon_module
from subfleet import hooks, render
from subfleet.procs import Containment
from tests.fake.notice_invariant import notice_mismatches
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401

#: What Codex left in `last.md` when the daemon stopped it on 2026-09-24.
INTERIM = b"I am checking the newer validation code before finalizing the review.\n"


def notice_lines(daemon, job_id: str) -> list[str]:
    notice, = daemon.store.query("SELECT text FROM notices WHERE job_id=?", (job_id,))
    return notice["text"].splitlines()


def dashes(job_id: str, state: str, rc: int) -> str:
    return f"{job_id}: {state}; rc={rc}; deliverable=-; out=-"


# --- the header is the job row (C-15.1) ---------------------------------------

def test_c15_1_c7_2_acceptance_first_names_the_deliverable_and_out(state_daemon):
    """C-4.3, C-7.2, C-8.3, C-15.1 an accepted job's header names its deliverable and
    `-o` path, and its summary the attempt that was accepted."""
    daemon, harness = state_daemon
    out = harness.root / "export.md"
    job_id, attempt, adir = reserve(daemon, harness, out_path=str(out))
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    header, summary = notice_lines(daemon, job_id)
    assert header == (f"{job_id}: succeeded; rc=0; deliverable={adir / 'deliverable.md'}; "
                      f"out={out}")
    assert summary == "attempt a1: ok, rc=0: fake provider succeeded; unattested"
    assert out.read_bytes() == b"fixture result\n"


def test_c15_1_c7_2_cancel_first_names_no_file_and_says_the_output_was_not_accepted(state_daemon):
    """C-7.2, C-8.3, C-15.1 a job cancelled before acceptance is announced cancelled with
    rc 130 and no paths, whatever its attempt's class; the summary keeps the attempt's
    evidence and says its output was kept, not accepted, and the `-o` file not written."""
    daemon, harness = state_daemon
    out = harness.root / "export.md"
    job_id, attempt, adir = reserve(daemon, harness, out_path=str(out))
    finalizing = receipt_fixture(daemon, attempt, adir)
    daemon.dispatch("kill", {"job_id": job_id})
    daemon._finalize(finalizing)
    assert notice_lines(daemon, job_id) == [
        dashes(job_id, "cancelled", 130),
        "attempt a1: ok, rc=0: fake provider succeeded; unattested",
        f"output kept, not accepted: {adir / 'deliverable.md'}; -o {out} was not written",
    ]
    assert not out.exists()


def test_c15_1_a_failed_attempt_names_its_class_and_rc_under_the_jobs(state_daemon):
    """C-9.2, C-15.1 a failed job's header is the job's rc; the attempt's class is the summary's."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1)
    daemon._finalize(receipt_fixture(daemon, attempt, adir, rc=1, stdout=b"partial\n"))
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1)
    assert notice_lines(daemon, job_id) == [
        dashes(job_id, "failed", 1),
        "attempt a1: unknown, rc=1: fake provider failed; unattested",
        f"output kept, not accepted: {adir / 'deliverable.md'}",
    ]


def test_c15_1_a_lost_attempt_is_announced_lost_with_the_jobs_rc(state_daemon):
    """C-4.4, C-15.1 a loss without a receipt: `lost; rc=125`, the attempt's rc unknown."""
    daemon, harness = state_daemon
    job_id, attempt, _ = reserve(daemon, harness, max_attempts=1)
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=42001,
                                pgid=42001, boot_id="unit-test-boot", proc_start="unit-test-start")
    daemon._process_attempt(attempt["attempt_id"])
    assert notice_lines(daemon, job_id) == [
        dashes(job_id, "lost", 125),
        "attempt a1: unknown, rc=-: guardian lost without exit receipt; unattested",
    ]


def test_c15_1_c7_4_a_queued_job_cancelled_before_launch(state_daemon):
    """C-7.4, C-15.1 the kill of a queued job: `cancelled; rc=130`, never class `unknown`."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args(out_path=str(harness.root / "x.md")))["job_id"]
    daemon.dispatch("kill", {"job_id": job_id})
    assert notice_lines(daemon, job_id) == [dashes(job_id, "cancelled", 130), "cancelled before launch"]


@pytest.mark.parametrize("cancel", [False, True])
def test_c15_1_c6_12_a_job_refused_at_admission(state_daemon, cancel):
    """C-6.12, C-15.1 a refusal fails the job with its rc (2 here), or cancels it with 130
    when a cancel came first; the header is whichever the row says, the message the summary."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    if cancel:
        daemon.store.update_job(job_id, cancel_requested_at=daemon_module.utcnow())
    daemon._fail_queued(daemon.store.get_job(job_id), "refused at admission: RouteError: fixture",
                        rc=2, kind="job.route_refused")
    state, rc = ("cancelled", 130) if cancel else ("failed", 2)
    assert (daemon.store.get_job(job_id)["state"], daemon.store.get_job(job_id)["rc"]) == (state, rc)
    assert notice_lines(daemon, job_id) == [dashes(job_id, state, rc),
                                            "refused at admission: RouteError: fixture"]


def test_c15_1_c6_8_a_workspace_that_cannot_be_prepared(state_daemon):
    """C-6.8, C-15.1 a non-transient workspace failure: `failed; rc=1` and git's own words."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._workspace_failed(daemon.store.get_job(job_id), ValueError("fixture: not a repository"))
    assert notice_lines(daemon, job_id) == [
        dashes(job_id, "failed", 1),
        "workspace preparation failed: ValueError: fixture: not a repository"]


def test_c15_1_c23_55_a_skipped_revive_is_announced_failed_with_rc_7(state_daemon):
    """C-23.55, C-15.1 the skipped revive is `failed; rc=7`, not a `refused` class."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    with daemon.store.transaction("job.revive_skipped", job_id=job_id) as tx:
        daemon._skip_revive(tx, daemon.store.get_job(job_id), "other-revive")
    header, summary = notice_lines(daemon, job_id)
    assert header == dashes(job_id, "failed", 7)
    assert summary.startswith("skipped: session fake-session already has a live revive (other-revive)")


@pytest.mark.parametrize("cancel", [False, True])
def test_c15_1_c4_2_an_attempt_that_never_launched(state_daemon, cancel):
    """C-4.2 reserved, C-15.1 the last attempt ends without launching: the job's state and rc."""
    daemon, harness = state_daemon
    job_id, attempt, _ = reserve(daemon, harness, max_attempts=1)
    if cancel:
        daemon.store.update_job(job_id, cancel_requested_at=daemon_module.utcnow())
    daemon._process_attempt(attempt["attempt_id"])
    state, rc = ("cancelled", 130) if cancel else ("failed", 1)
    assert notice_lines(daemon, job_id) == [dashes(job_id, state, rc), "reserved-no-launch"]


@pytest.mark.parametrize("cancel", [False, True])
def test_c15_1_c5_7_a_quarantined_attempt_names_the_jobs_state_not_the_attempts_rc(
        state_daemon, monkeypatch, cancel):
    """C-4.2 starting, C-5.7, C-15.1 quarantine makes the job `lost` 125, or `cancelled` 130;
    the header said `unknown; rc=None` before 2026-09-24."""
    daemon, harness = state_daemon
    job_id, attempt, _ = reserve(daemon, harness)
    if cancel:
        daemon.store.update_job(job_id, cancel_requested_at=daemon_module.utcnow())
    daemon.store.update_attempt(attempt["attempt_id"], state="starting", guardian_pid=42001)
    daemon._starting_deadlines[attempt["attempt_id"]] = 0
    # A cancelled attempt is killed first, and an uninspectable group ends that
    # kill in quarantine; nothing here needs the grace windows to pass.
    daemon.term_grace_s = daemon.kill_settle_s = 0
    monkeypatch.setattr(daemon_module.procs, "containment",
                        lambda *args, **kwargs: Containment(unverifiable=True))
    daemon._process_attempt(attempt["attempt_id"])
    header, summary = notice_lines(daemon, job_id)
    assert header == (dashes(job_id, "cancelled", 130) if cancel else dashes(job_id, "lost", 125))
    assert summary.startswith("quarantined: ") and "unverifiable" in summary


def test_c15_1_a_notice_before_the_terminal_state_is_refused(state_daemon):
    """C-15.1 the header is read from the row the transaction made terminal; a caller that
    writes the notice first is a defect, refused before anything is written."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    with pytest.raises(RuntimeError, match="before its terminal state"):
        with daemon.store.transaction("fixture.early_notice", job_id=job_id) as tx:
            daemon._notice(tx, daemon.store.get_job(job_id), "too early")
    assert daemon.store.list_notices() == []
    assert daemon.store.get_job(job_id)["state"] == "queued"


@pytest.mark.parametrize("cancel_first", [False, True])
def test_c15_1_the_hook_fallback_renders_the_row_as_the_notice_does(state_daemon, cancel_first):
    """C-15.1, C-15.2 layer 2: `hooks.job_summary` over the job row the daemon returns
    has the notice's header, character for character, in both commit orders."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, out_path=str(harness.root / "export.md"))
    finalizing = receipt_fixture(daemon, attempt, adir)
    if cancel_first:
        daemon.dispatch("kill", {"job_id": job_id})
    daemon._finalize(finalizing)
    from subfleet import protocol
    row, = daemon.wait(protocol.WaitArgs(job_ids=[job_id], deadline_s=0))["jobs"]
    assert hooks.job_summary(row, daemon.root).splitlines()[0] == notice_lines(daemon, job_id)[0]
    assert render.notice_header(row, daemon.root) == notice_lines(daemon, job_id)[0]


# --- a signalled attempt is never ok (C-9.2) ----------------------------------

def _codex_exits_zero_on_sigterm(monkeypatch, adir: Path) -> list[int]:
    """Stand in for the process group the daemon signals.

    No process exists; `signal_group` is where the daemon's SIGTERM would reach
    the provider, so the stub does what Codex did on 2026-09-24: its last
    interim message is the deliverable and the guardian records exit 0.
    """
    sent: list[int] = []

    def signal_group(pgid, sig, **identity):
        sent.append(int(sig))
        if sig == signal.SIGTERM:
            (adir / "stdout").write_bytes(INTERIM)
            (adir / "stderr").write_bytes(b"")
            (adir / "exit.json").write_text(json.dumps({
                "rc": 0, "signal": None, "wall_s": 12.0, "child_pid": 42002,
                "finished_at": daemon_module.utcnow()}))
        return True

    monkeypatch.setattr(daemon_module.procs, "signal_group", signal_group)
    return sent


def _running(daemon, attempt) -> None:
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=42001,
                                pgid=42001, child_pid=42002, boot_id="unit-test-boot",
                                proc_start="unit-test-start", started_at=daemon_module.utcnow())


@pytest.mark.parametrize("stopped_by", ["operator", "max_wall_s"])
def test_c9_2_c15_1_an_attempt_the_daemon_signalled_is_never_ok(state_daemon, monkeypatch, stopped_by):
    """C-9.2, C-7.2, C-8.3, C-15.1: the 2026-09-24 incident in process.

    A running attempt is stopped by an operator's kill or by the wall limit; the
    provider exits 0 after the SIGTERM with a non-empty deliverable. The attempt is
    `unknown` with the killed detail and the adapter's `ok` kept as evidence, the job
    `cancelled` with rc 130, nothing accepted, no export artifact and no `-o` file,
    and the notice says so."""
    daemon, harness = state_daemon
    out = harness.root / "export.md"
    job_id, attempt, adir = reserve(daemon, harness, out_path=str(out))
    _running(daemon, attempt)
    sent = _codex_exits_zero_on_sigterm(monkeypatch, adir)
    if stopped_by == "operator":
        assert daemon.dispatch("kill", {"job_id": job_id})["status"] == "cancel requested"
    else:
        daemon.store.update_job(job_id, started_at="2026-09-24T07:00:00Z", max_wall_s=60)
    daemon._process_attempt(attempt["attempt_id"])            # the kill: SIGTERM, receipt, finalizing
    assert sent == [int(signal.SIGTERM)]
    daemon._process_attempt(attempt["attempt_id"])            # finalization
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"], job["accepted_attempt_id"]) == ("cancelled", 130, None)
    row = daemon.store.get_attempt(attempt["attempt_id"])
    assert (row["state"], row["rc"], row["killed_by"]) == ("interrupted", 0, stopped_by)
    assert row["outcome_class"] == "unknown"
    detail = (f"stopped by {stopped_by}: exit 0 after the daemon's signal "
              f"is not a finished deliverable")
    assert row["outcome_detail"] == detail
    evidence = json.loads(row["evidence_json"])
    assert evidence["provider_verdict"] == {"class": "ok", "detail": "fake provider succeeded",
                                            "killed_by": stopped_by}
    # C-9.2: the adapter's own verdict stays in finalization.json for replay.
    assert json.loads((adir / "finalization.json").read_text())["outcome"]["cls"] == "ok"
    roles = {artifact["role"] for artifact in daemon.store.list_artifacts(attempt["attempt_id"])}
    assert "deliverable" in roles and "export" not in roles
    assert not out.exists()
    assert not daemon.store.query("SELECT 1 FROM events WHERE kind='job.exported' AND job_id=?", (job_id,))
    assert notice_lines(daemon, job_id) == [
        dashes(job_id, "cancelled", 130),
        f"attempt a1: unknown, rc=0: {detail}; unattested",
        f"output kept, not accepted: {adir / 'deliverable.md'}; -o {out} was not written",
    ]


def test_c9_2_c4_3_the_killed_downgrade_survives_finalization_replay(state_daemon, monkeypatch):
    """C-4.3, C-9.2 a finalization interrupted after classification replays to the same
    class: the downgrade is recomputed from the attempt row, not frozen with the verdict."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness)
    _running(daemon, attempt)
    _codex_exits_zero_on_sigterm(monkeypatch, adir)
    daemon.dispatch("kill", {"job_id": job_id})
    daemon._process_attempt(attempt["attempt_id"])
    notice = daemon._notice

    def crash(*args):
        raise RuntimeError("daemon died before the terminal commit")

    monkeypatch.setattr(daemon, "_notice", crash)
    with pytest.raises(RuntimeError, match="terminal commit"):
        daemon._process_attempt(attempt["attempt_id"])
    assert (adir / "finalization.json").is_file()
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "finalizing"
    monkeypatch.setattr(daemon, "_notice", notice)
    daemon._process_attempt(attempt["attempt_id"])
    row = daemon.store.get_attempt(attempt["attempt_id"])
    assert (row["state"], row["outcome_class"]) == ("interrupted", "unknown")
    assert notice_lines(daemon, job_id)[0] == dashes(job_id, "cancelled", 130)


def test_c9_2_recovery_of_a_dead_guardians_group_is_not_ok_either(state_daemon, monkeypatch):
    """C-4.2 running, C-9.2 an attempt signalled by recovery (a dead guardian with writers
    left) whose receipt says 0 is not accepted: no cancel was asked, so the job fails."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1)
    _running(daemon, attempt)
    _codex_exits_zero_on_sigterm(monkeypatch, adir)
    daemon._kill_attempt(daemon.store.get_attempt(attempt["attempt_id"]), lost=True)
    daemon._process_attempt(attempt["attempt_id"])
    row = daemon.store.get_attempt(attempt["attempt_id"])
    assert (row["killed_by"], row["outcome_class"]) == ("recovery", "unknown")
    job = daemon.store.get_job(job_id)
    assert job["state"] == "failed" and job["accepted_attempt_id"] is None
    assert notice_lines(daemon, job_id)[0] == dashes(job_id, "failed", job["rc"])


def test_c9_2_an_attempt_that_finished_before_the_kill_reached_it_stays_ok(state_daemon):
    """C-7.2, C-9.2 a cancel the daemon never acted on signals nothing: an attempt whose
    receipt arrived first keeps its `ok` (the job is still cancelled, C-7.2)."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness)
    finalizing = receipt_fixture(daemon, attempt, adir)
    daemon.dispatch("kill", {"job_id": job_id})
    daemon._finalize(finalizing)
    row = daemon.store.get_attempt(attempt["attempt_id"])
    assert (row["killed_by"], row["outcome_class"], row["state"]) == (None, "ok", "interrupted")
    assert "provider_verdict" not in json.loads(row["evidence_json"])


def test_c12_6_the_empty_deliverable_override_keeps_the_adapters_verdict_too(state_daemon):
    """C-12.6, C-9.2 whenever the daemon overrides the adapter's class, the verdict it
    overrode is kept in the attempt's evidence."""
    daemon, harness = state_daemon
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=1)
    daemon._finalize(receipt_fixture(daemon, attempt, adir, stdout=b""))
    row = daemon.store.get_attempt(attempt["attempt_id"])
    assert (row["outcome_class"], row["outcome_detail"]) == ("unknown", "empty deliverable with rc 0")
    assert json.loads(row["evidence_json"])["provider_verdict"] == {
        "class": "ok", "detail": "fake provider succeeded", "killed_by": None}
    assert notice_lines(daemon, job_id) == [
        dashes(job_id, "failed", 0),
        "attempt a1: unknown, rc=0: empty deliverable with rc 0; unattested"]


# --- the invariant the harness now checks at close ---------------------------

def test_c15_1_the_close_check_catches_a_notice_that_disagrees_with_its_job(state_daemon):
    """C-15.1 `notice_mismatches`, which `Harness.close` and `state_daemon` run after every
    fake-daemon test, reports a header whose state or rc is not the job row's."""
    daemon, harness = state_daemon
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon.dispatch("kill", {"job_id": job_id})
    assert notice_mismatches(harness.rows) == []
    other = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon.dispatch("kill", {"job_id": other})
    with daemon.store.transaction("fixture.forged_notice", job_id=other) as tx:
        tx.execute("UPDATE notices SET text=? WHERE job_id=?",
                   (f"{other}: ok; rc=0; deliverable=/x; out=/y\nforged", other))
    problems = notice_mismatches(harness.rows)
    assert len(problems) == 1 and other in problems[0]
    # A v1 notice the importer carried has its own text; it is not a v2 header.
    with daemon.store.transaction("fixture.imported_notice", job_id=other) as tx:
        tx.execute("UPDATE notices SET text=? WHERE job_id=?", ("run finished", other))
    assert notice_mismatches(harness.rows) == []
