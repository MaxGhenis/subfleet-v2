"""Round 3 (D-KILL3 review r3, P2): a finished job's cwd hold is a reader row.

The hold exists so that retention never removes a folder under a live cwd
(C-5.5, C-13.4). Kept as the job's exclusive `worktree:` key, it also refused
`subfleet resume` of the job at submit (`reason='worktree-lease'`, with nothing
to resolve), and held any writable turn into its worktree at admission
(`lease-held` on that key, observed at 9b423bd26), for as long as someone had a
shell there. It is now `worktree-read:<folder>:<job id>` held by `cwd:<job id>`
(`folders.CWD`): it excludes no writer, retention honours it as it honours any
reader row, and only the cwd worker releases it.

The source jobs here run the real submit, admission, finalization and export;
only the provider and the kernel's cwd read are fixtures (`observed`).
"""

from pathlib import Path

import pytest

from subfleet import daemon as daemon_module, folders, procs, protocol, retention
from subfleet.adapters.base import AdapterError
from tests.fake.test_admission_liveness import _live
from tests.fake.test_resume_contract import finish_reserved, finished_source, resume_job
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_turn_wait_reasons import message_in

OBSERVER = 4500


@pytest.fixture
def observed(state_daemon, monkeypatch):
    """`state_daemon` with one cwd observer in every job-owned worktree, which the
    test moves (`cwd[0]`), and no 30 s wait for it at finalization (C-5.9)."""
    daemon, harness = state_daemon
    cwd = [frozenset({OBSERVER})]
    monkeypatch.setattr(procs, "worktree_cwd_pids", lambda worktree: cwd[0])
    monkeypatch.setattr(daemon_module, "CWD_SETTLE_S", 0)
    return daemon, harness, cwd


def leases(daemon) -> dict[str, str]:
    return {row["lease_key"]: row["holder"] for row in daemon.store.list_leases()}


def kept_key(worktree: str, job_id: str) -> str:
    return folders.turn_key(worktree, job_id, writable=False)


def finished_writer(daemon, harness) -> tuple[str, str]:
    """A writable source that ends `succeeded` while the observer is in its worktree:
    the review's executed input."""
    source_id, _ = finished_source(daemon, harness, writable=True)
    job = daemon.store.get_job(source_id)
    assert job["state"] == "succeeded" and job["accepted_attempt_id"]
    return source_id, job["worktree"]


def sweep(daemon) -> dict:
    return retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                 holders=lambda watches, **_: {})


def tick(daemon, job_id):
    """One due recheck of the cwd worker (C-5.9), whatever its clock says."""
    daemon._cwd_recheck_next = {}
    if job_id in daemon._pending_worktree_holds():
        daemon._release_worktree_hold(job_id)


def test_r3_the_kept_worktree_is_one_reader_row_and_no_writer_key(observed):
    """C-5.5: every other lease went with the export; the worktree is kept as a reader
    row on the same folder, held by `cwd:<job id>`, with the observer named."""
    daemon, harness, cwd = observed
    source_id, worktree = finished_writer(daemon, harness)
    assert leases(daemon) == {kept_key(worktree, source_id): folders.cwd_holder(source_id)}
    assert daemon._pending_worktree_holds() == [source_id]
    notice = daemon.store.query("SELECT text FROM notices WHERE job_id=?", (source_id,))[-1]["text"]
    assert f"worktree retained: pid {OBSERVER}: cwd in worktree" in notice


def test_r3_resume_is_admitted_while_a_finished_sources_observer_lives(observed):
    """r3's executed input: refused at 9b423bd26 with exit 7, `worktree-lease`."""
    daemon, harness, cwd = observed
    source_id, worktree = finished_writer(daemon, harness)
    resumed = resume_job(daemon, harness, source_id)
    daemon._admit()
    assert daemon.store.get_job(resumed)["state"] == "running", daemon._holds.get(resumed)
    held = leases(daemon)
    assert held[folders.exclusive_key(folders.canonical(worktree))] == resumed
    assert held[kept_key(worktree, source_id)] == folders.cwd_holder(source_id)
    assert cwd[0] == {OBSERVER}


def test_r3_a_writable_turn_into_the_kept_worktree_is_admitted(observed):
    """Held `lease-held` on the kept key at 9b423bd26; the reader row holds no turn back (C-24.5)."""
    daemon, harness, cwd = observed
    source_id, worktree = finished_writer(daemon, harness)
    _, _, turn = message_in(daemon, harness, "Inspecting the kept tree", workspace=worktree)
    daemon._admit_turns()
    assert _live(daemon, turn), daemon._holds.get(turn)
    held = leases(daemon)
    assert held[kept_key(worktree, source_id)] == folders.cwd_holder(source_id)
    assert any(key.startswith(folders.TURN) and holder == turn for key, holder in held.items())


def test_r3_retention_keeps_the_folder_while_the_reader_row_lives_and_takes_it_after(observed):
    """Invariant 1: the row alone keeps the tree; released, the tree is retired."""
    daemon, harness, cwd = observed
    source_id, worktree = finished_writer(daemon, harness)
    daemon.store.acknowledge_notices("fake-session", [row["notice_id"] for row in daemon.store.list_notices()])
    assert folders.turn_holds(daemon.store.query, worktree, (folders.READER,)) == [
        (kept_key(worktree, source_id), folders.cwd_holder(source_id))]
    for observers in ({OBSERVER}, frozenset()):
        # With the observer there, and after it left but before the worker's recheck.
        cwd[0] = frozenset(observers)
        result = sweep(daemon)
        assert source_id not in result["pruned"] and Path(worktree).is_dir(), result
        assert retention._pin_reasons(daemon.store, set(), None, only=source_id,
                                      root=daemon.root).get(source_id) == "turn-folder"
    tick(daemon, source_id)
    assert leases(daemon) == {}
    result = sweep(daemon)
    assert source_id in result["pruned"] and not Path(worktree).exists(), result


def test_r3_a_second_writer_is_still_refused_while_the_resume_holds_its_key(observed):
    """Invariant 3: exclusivity among live writers is C-6.5's, unchanged. A detached
    in-place writer is refused at submit; a writable turn is held at admission on the
    resume's key (as 9b423bd26 held one on the source's kept key: not refused)."""
    daemon, harness, cwd = observed
    source_id, worktree = finished_writer(daemon, harness)
    resumed = resume_job(daemon, harness, source_id)
    daemon._admit()
    assert daemon.store.get_job(resumed)["state"] == "running"
    writer_key = folders.exclusive_key(folders.canonical(worktree))
    with pytest.raises(AdapterError) as refused:
        daemon.submit(protocol.SubmitArgs(**harness.submit_args(
            workdir=worktree, sandbox="workspace-write", in_place=True)))
    # Submit's live-writer check names the resume before any lease is read (C-6.5).
    assert refused.value.code == 7 and refused.value.reason == "worktree-writer"
    assert refused.value.details["job_id"] == resumed
    _, _, turn = message_in(daemon, harness, "Writing beside the resume", workspace=worktree)
    daemon._admit_turns()
    assert not _live(daemon, turn)
    hold = daemon._holds[turn]
    assert hold["reason"] == "lease-held" and writer_key in hold["leases"], hold
    assert [key for key in leases(daemon) if key.startswith(folders.TURN)] == []
    assert leases(daemon)[writer_key] == resumed


@pytest.mark.parametrize("resume", ["running", "finished"])
def test_r3_the_old_row_goes_when_the_observer_leaves_and_never_the_resumes(observed, resume):
    """Release order: the source's row goes when its observer leaves, whether or not the
    resume still runs; the worker never touches the resume's own rows."""
    daemon, harness, cwd = observed
    source_id, worktree = finished_writer(daemon, harness)
    resumed = resume_job(daemon, harness, source_id)
    daemon._admit()
    own = {key: holder for key, holder in leases(daemon).items() if holder == resumed}
    assert folders.exclusive_key(folders.canonical(worktree)) in own
    if resume == "finished":
        finish_reserved(daemon, resumed)
        assert daemon.store.get_job(resumed)["state"] == "succeeded"
        own = {}
    tick(daemon, source_id)                     # the observer is still there
    assert leases(daemon)[kept_key(worktree, source_id)] == folders.cwd_holder(source_id)
    assert {key: holder for key, holder in leases(daemon).items() if holder == resumed} == own
    cwd[0] = frozenset()
    tick(daemon, source_id)
    assert kept_key(worktree, source_id) not in leases(daemon)
    assert {key: holder for key, holder in leases(daemon).items() if holder == resumed} == own
    assert source_id not in daemon._pending_worktree_holds()


def test_r3_the_reader_row_survives_a_restart_and_the_worker_still_releases_it(observed):
    daemon, harness, cwd = observed
    source_id, worktree = finished_writer(daemon, harness)
    daemon.close()
    restarted = daemon_module.Daemon(harness.root)
    try:
        assert leases(restarted) == {kept_key(worktree, source_id): folders.cwd_holder(source_id)}
        assert restarted._pending_worktree_holds() == [source_id]
        restarted._release_worktree_hold(source_id)
        assert leases(restarted) == {kept_key(worktree, source_id): folders.cwd_holder(source_id)}
        cwd[0] = frozenset()
        tick(restarted, source_id)
        assert leases(restarted) == {}
        resumed = resume_job(restarted, harness, source_id)
        assert restarted.store.get_job(resumed)["state"] == "queued"
    finally:
        restarted.close()
