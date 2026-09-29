"""C-13.1, C-13.4: a salvage that cannot succeed ends the attempt and frees its lane.

Before, a finalizing attempt whose salvage failed raised to the worker, which
tried it again every 60 s for as long as the daemon ran: the attempt never left
`finalizing` and held its lane slot (2026-09-27, codex-3 and codex-5, for 2.5 h
and 5 h). Now a transient failure is tried `SALVAGE_TRIES` times in all and any
other failure is recorded at once; finalization then goes on without a salvage
ref and the worktree is kept.
"""
from __future__ import annotations

import json
import os

import pytest

from subfleet import daemon as daemon_module
from subfleet import salvage as salvage_module
from subfleet.adapters.registry import register
from subfleet.contracts import Outcome, OutcomeClass
from subfleet.daemon import SALVAGE_SKIPPED_SHOWN, SALVAGE_TRIES
from subfleet.salvage import SalvageError, SalvageResult
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401 (a fixture)
from tests.fake_adapter import FakeAdapter
from tests.fake.test_workspace_contract import repository
from tests.unit.test_salvage import git
from tests.unit.test_salvage_unindexable import ROOT, committed_repository, fake_add, on_a_branch_whose_name_is_not_utf8

#: What git's `error()` prints for a file it cannot read whose name is not UTF-8
#: (Linux allows such names; APFS refuses them, so `add -A` is faked for these).
NOT_UTF8_STDERR = (b'error: open("caf\xe9.txt"): Permission denied\n'
                   b"error: unable to index file 'caf\xe9.txt'\nfatal: adding files failed\n")
NOT_UTF8_ERROR = ('salvage failed: git add failed: error: open("caf\\xe9.txt"): Permission denied\n'
                  "error: unable to index file 'caf\\xe9.txt'\nfatal: adding files failed")


def lane_leases(daemon):
    return [row for row in daemon.store.list_leases() if row["lease_key"].startswith("lane:")]


def empty_repository(path):
    """What the incident's fixture left: a repository with a file and no commit."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "inside.txt").write_text("never committed\n")


def job_notices(daemon, job_id):
    return [row["text"] for row in daemon.store.list_notices() if row["job_id"] == job_id]


def events(daemon, job_id, kind):
    return [json.loads(row["data_json"]) for row in daemon.store.list_events(job_id) if row["kind"] == kind]


def finalizing(daemon, harness, *, change=True):
    workdir = repository(daemon, harness)
    job_id, attempt, adir = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    if change:
        (workdir / "tracked.txt").write_text("provider progress\n")
    return workdir, job_id, receipt_fixture(daemon, attempt, adir), adir


def test_c13_1_a_salvage_that_cannot_succeed_is_recorded_and_the_attempt_ends(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    calls = []

    def refuse(*args, **kwargs):
        calls.append(1)
        raise SalvageError("git add failed: fatal: adding files failed")
    monkeypatch.setattr(daemon_module, "salvage", refuse)
    daemon._finalize(attempt)
    assert calls == [1]                                       # not transient: recorded at once
    job = daemon.store.get_job(job_id)
    assert job["state"] == "succeeded" and job["rc"] == 0     # the provider's result stands
    assert lane_leases(daemon) == []                          # the lane is free
    row = daemon.store.get_attempt(attempt["attempt_id"])
    assert row["state"] == "succeeded"
    evidence = json.loads(row["evidence_json"])
    assert evidence["salvage_error"] == "salvage failed: git add failed: fatal: adding files failed"
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt == {"result": None, "checkpoint": None, "error": evidence["salvage_error"], "skipped": []}
    assert not [r for r in daemon.store.list_artifacts(attempt["attempt_id"]) if r["role"] == "salvage"]
    notice = daemon.store.list_notices()[-1]["text"]
    assert "salvage failed" in notice and f"the worktree is kept: {workdir}" in notice
    assert (workdir / "tracked.txt").read_text() == "provider progress\n"   # nothing was touched


def test_c13_1_a_transient_failure_is_tried_again_then_recorded(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    _, job_id, attempt, _ = finalizing(daemon, harness)
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        raise SalvageError("git add timed out after 60 s", transient=True)
    monkeypatch.setattr(daemon_module, "salvage", slow)
    for tries in range(1, SALVAGE_TRIES):
        with pytest.raises(SalvageError):
            daemon._finalize(attempt)                         # the worker's backoff tries it again
        assert len(calls) == tries
        assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "finalizing"
    daemon._finalize(attempt)
    assert len(calls) == SALVAGE_TRIES
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert "timed out" in evidence["salvage_error"]
    assert attempt["attempt_id"] not in daemon._salvage_failures


def test_c13_1_a_transient_failure_that_clears_salvages_normally(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    workdir, job_id, attempt, _ = finalizing(daemon, harness)
    real, failures = daemon_module.salvage, [SalvageError("git add timed out after 60 s", transient=True)]

    def once(*args, **kwargs):
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)
    monkeypatch.setattr(daemon_module, "salvage", once)
    with pytest.raises(SalvageError):
        daemon._finalize(attempt)
    daemon._finalize(attempt)
    row = daemon.store.get_attempt(attempt["attempt_id"])
    assert "salvage_error" not in json.loads(row["evidence_json"])
    [artifact] = [r for r in daemon.store.list_artifacts(attempt["attempt_id"]) if r["role"] == "salvage"]
    assert git(workdir, "show", f"{artifact['path']}:tracked.txt") == "provider progress"
    assert attempt["attempt_id"] not in daemon._salvage_failures


def test_c13_1_the_case_that_held_the_lanes_now_finalizes_with_a_snapshot(state_daemon):
    """No stand-in: a real empty repository under untracked scratch, as the reviewers left it.
    The snapshot holds everything else, and the evidence and the notice say what it left out
    and where that still is (review of c1f95838, F2: only the receipt and the log said)."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    empty_repository(workdir / ".review-scratch" / "pytest" / "repo ")
    daemon._finalize(attempt)
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt["error"] is None and receipt["result"]["skipped"] == [".review-scratch/pytest/repo /"]
    assert receipt["skipped"] == receipt["result"]["skipped"]
    assert git(workdir, "show", f"{receipt['result']['ref']}:tracked.txt") == "provider progress"
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert "salvage_error" not in evidence
    assert evidence["salvage_skipped"] == {"count": 1, "paths": [".review-scratch/pytest/repo /"]}
    [notice] = job_notices(daemon, job_id)
    assert ("\nsalvage left out 1 nested repository with no commit, kept only in the worktree: "
            f"'.review-scratch/pytest/repo /'; the worktree is kept: {workdir}") in notice


def test_c13_1_when_only_a_left_out_repository_changed_the_notice_still_says_so(state_daemon):
    """Nothing else changed, so no ref is written, and the repository exists only in the
    worktree: exactly the case a caller must hear about."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness, change=False)
    empty_repository(workdir / "newpkg")
    daemon._finalize(attempt)
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt == {"result": None, "checkpoint": git(workdir, "rev-parse", "HEAD"), "error": None,
                       "skipped": ["newpkg/"]}
    assert not [r for r in daemon.store.list_artifacts(attempt["attempt_id"]) if r["role"] == "salvage"]
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_skipped"] == {"count": 1, "paths": ["newpkg/"]}
    [notice] = job_notices(daemon, job_id)
    assert notice.endswith("\nsalvage left out 1 nested repository with no commit, kept only in the worktree: "
                           f"'newpkg/'; the worktree is kept: {workdir}")


def test_c13_1_the_evidence_and_the_notice_name_the_first_few_and_count_them_all(state_daemon):
    daemon, harness = state_daemon
    workdir, job_id, attempt, _ = finalizing(daemon, harness)
    names = [f"scratch/r{i}/" for i in range(SALVAGE_SKIPPED_SHOWN + 2)]
    for name in names:
        empty_repository(workdir / name)
    daemon._finalize(attempt)
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_skipped"] == {"count": len(names), "paths": names[:SALVAGE_SKIPPED_SHOWN]}
    [notice] = job_notices(daemon, job_id)
    shown = ", ".join(f"'{name}'" for name in names[:SALVAGE_SKIPPED_SHOWN])
    assert f"left out {len(names)} nested repositories with no commit, kept only in the worktree: {shown}, ...;" in notice


def test_c13_1_a_left_out_name_that_is_not_utf8_does_not_stop_the_receipt(state_daemon, monkeypatch):
    """`os.fsdecode` carries such a name as surrogates, which `json_bytes` cannot encode:
    a receipt that cannot be written raised on every try, the incident again. It is
    written with the bytes escaped. (APFS refuses such names, so salvage is faked.)"""
    daemon, harness = state_daemon
    _, job_id, attempt, adir = finalizing(daemon, harness)
    with pytest.raises(UnicodeEncodeError):
        daemon_module.json_bytes({"skipped": ["caf\udce9/"]})

    def left_out(*args, left_out, **kwargs):
        left_out.append("caf\udce9/")
        return SalvageResult("refs/subfleet-salvage/x", "c" * 40, "t" * 40, "b" * 40, ("caf\udce9/",))
    monkeypatch.setattr(daemon_module, "salvage", left_out)
    daemon._finalize(attempt)
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt["skipped"] == receipt["result"]["skipped"] == ["caf\\xe9/"]
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_skipped"]["paths"] == ["caf\\xe9/"]
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []


def test_c13_1_a_failure_that_quotes_a_name_that_is_not_utf8_is_recorded(state_daemon, monkeypatch):
    """Review of cda4c161, N1: only `add -A` is faked; every other git call, the receipt,
    the evidence and the notice are real. git quotes the name in its own bytes, the
    error carried it as a surrogate, and `json_bytes` could not write the receipt: the
    worker tried again for ever, the attempt held its lane, the incident again."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    fake_add(monkeypatch, 128, NOT_UTF8_STDERR)
    daemon._finalize(attempt)
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt == {"result": None, "checkpoint": None, "error": NOT_UTF8_ERROR, "skipped": []}
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_error"] == NOT_UTF8_ERROR
    [notice] = job_notices(daemon, job_id)
    assert notice.endswith(f"\n{NOT_UTF8_ERROR}; the worktree is kept: {workdir}")


def test_c13_1_a_quarantine_release_records_a_failure_that_quotes_such_a_name(state_daemon, monkeypatch):
    """Review of cda4c161, N1: the release's salvage (`retry=False`) failed the same way,
    so `kill --confirm-dead` could never finish."""
    from subfleet import protocol
    from subfleet.procs import Containment
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id, attempt, _ = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    (workdir / "tracked.txt").write_text("provider progress\n")
    daemon._quarantine(attempt, Containment(marker_pids=frozenset({42099})), "escaped fixture")
    monkeypatch.setattr(daemon, "_contain", lambda attempt: Containment())
    fake_add(monkeypatch, 128, NOT_UTF8_STDERR)
    daemon._resolve_quarantine(daemon.store.get_attempt(attempt["attempt_id"]),
                               protocol.KillArgs(job_id, confirm_dead=True))
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "lost"
    event = json.loads(daemon.store.one("SELECT * FROM events WHERE kind='quarantine.confirmed_dead'")["data_json"])
    assert event["salvage_error"] == NOT_UTF8_ERROR
    _, released = job_notices(daemon, job_id)
    assert released.split("\n", 2)[1:] == ["released from quarantine (attempt a1)",
                                            f"{NOT_UTF8_ERROR}; the worktree is kept: {workdir}"]


def test_c13_1_a_job_on_a_branch_whose_name_is_not_utf8_is_admitted_and_salvaged(state_daemon):
    """Real git, no fake: `git_branch` read `symbolic-ref`'s output as strict UTF-8 and raised
    `UnicodeDecodeError`, at admission's main/master check and in salvage, and neither
    catches it: the admission pass, then finalization, raised on every try."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    on_a_branch_whose_name_is_not_utf8(workdir)
    job_id, attempt, adir = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    assert attempt["state"] == "reserved"
    (workdir / "tracked.txt").write_text("provider progress\n")
    attempt = receipt_fixture(daemon, attempt, adir)
    daemon._finalize(attempt)
    [artifact] = [r for r in daemon.store.list_artifacts(attempt["attempt_id"]) if r["role"] == "salvage"]
    assert artifact["path"].startswith("refs/subfleet-salvage/caf-")
    assert git(workdir, "show", f"{artifact['path']}:tracked.txt") == "provider progress"
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []


def test_c13_1_a_ref_lock_another_git_process_holds_is_tried_again(state_daemon):
    """Real git (review F5): a concurrent `gc` or `update-ref` holds the salvage ref's lock.
    That clears on its own, so the worker tries again rather than finalizing without a ref."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    name = f"task-example-{salvage_module._stamp(attempt['reserved_at'])}-a{attempt['seq']}"
    held = workdir / ".git" / "refs" / "subfleet-salvage" / f"{name}.lock"
    held.parent.mkdir(parents=True)
    held.touch()
    with pytest.raises(SalvageError, match="File exists") as caught:
        daemon._finalize(attempt)
    assert caught.value.transient and not (adir / "salvage.json").exists()
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "finalizing"
    held.unlink()
    daemon._finalize(attempt)
    [artifact] = [r for r in daemon.store.list_artifacts(attempt["attempt_id"]) if r["role"] == "salvage"]
    assert artifact["path"] == f"refs/subfleet-salvage/{name}"
    assert "salvage_error" not in json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])


def test_c13_1_a_replayed_finalization_reads_the_receipt_and_does_not_salvage_again(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    _, _, attempt, adir = finalizing(daemon, harness)
    (adir / "salvage.json").write_text(json.dumps({"result": None, "checkpoint": None,
                                                   "error": "salvage failed: recorded earlier"}))
    monkeypatch.setattr(daemon_module, "salvage", lambda *a, **k: pytest.fail("salvaged twice"))
    daemon._finalize(attempt)
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_error"] == "salvage failed: recorded earlier"


def test_c13_1_a_receipt_from_before_skipped_had_its_own_key_is_read(state_daemon, monkeypatch):
    """A replay across the upgrade reads c1f95838's receipt, which listed them in the result."""
    daemon, harness = state_daemon
    workdir, _, attempt, adir = finalizing(daemon, harness)
    (adir / "salvage.json").write_text(json.dumps({"result": {"ref": "refs/subfleet-salvage/x", "commit": "c" * 40,
                                                              "tree": "t" * 40, "baseline": "b" * 40,
                                                              "skipped": ["old/"]},
                                                   "checkpoint": None, "error": None}))
    monkeypatch.setattr(daemon_module, "salvage", lambda *a, **k: pytest.fail("salvaged twice"))
    daemon._finalize(attempt)
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_skipped"] == {"count": 1, "paths": ["old/"]}


def test_c13_1_a_snapshot_written_before_head_could_be_read_stands(state_daemon, monkeypatch):
    """Only the checkpoint read failed: the ref is recorded, the error names the checkpoint,
    and the notice does not claim the worktree was left unsaved."""
    daemon, harness = state_daemon
    workdir, _, attempt, adir = finalizing(daemon, harness)

    def no_head(*args, **kwargs):
        raise SalvageError("git rev-parse timed out after 60 s", transient=True)
    monkeypatch.setattr(daemon_module, "git_head", no_head)
    for _ in range(1, SALVAGE_TRIES):
        with pytest.raises(SalvageError):
            daemon._finalize(attempt)
    daemon._finalize(attempt)
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt["result"]["ref"].startswith("refs/subfleet-salvage/")
    assert receipt["checkpoint"] is None and receipt["error"].startswith("checkpoint failed: ")
    [artifact] = [r for r in daemon.store.list_artifacts(attempt["attempt_id"]) if r["role"] == "salvage"]
    assert git(workdir, "show", f"{artifact['path']}:tracked.txt") == "provider progress"
    notice = daemon.store.list_notices()[-1]["text"]
    assert "checkpoint failed" in notice and "the worktree is kept" not in notice


def test_c13_1_a_quarantine_release_records_a_failed_salvage_at_once(state_daemon, monkeypatch):
    """An operator's one-shot request is never offered again, so even a transient
    failure is recorded, and the attempt is released. `kill --confirm-dead` answered
    before the release ran and the job's notice went out at the quarantine, so the
    failure goes into the audit event, the attempt's evidence and one more notice
    (review F6: only the audit event had it)."""
    from subfleet import protocol
    from subfleet.procs import Containment
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id, attempt, _ = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    daemon._quarantine(attempt, Containment(marker_pids=frozenset({42099})), "escaped fixture")
    monkeypatch.setattr(daemon, "_contain", lambda attempt: Containment())
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        raise SalvageError("git add timed out after 60 s", transient=True)
    monkeypatch.setattr(daemon_module, "salvage", slow)
    stale = daemon.store.get_attempt(attempt["attempt_id"])
    daemon._resolve_quarantine(stale, protocol.KillArgs(job_id, confirm_dead=True))
    assert calls == [1]
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "lost"
    assert daemon.store.list_leases() == []
    event = json.loads(daemon.store.one("SELECT * FROM events WHERE kind='quarantine.confirmed_dead'")["data_json"])
    assert event["salvage_error"] == "salvage failed: git add timed out after 60 s"
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_error"] == event["salvage_error"]
    quarantined, released = job_notices(daemon, job_id)
    assert "quarantined: " in quarantined
    workdir = daemon.store.get_job(job_id)["workdir"]
    assert released.split("\n")[0] == quarantined.split("\n")[0]           # the job's C-15.1 header
    assert released.split("\n")[1:] == [
        "released from quarantine (attempt a1)",
        f"salvage failed: git add timed out after 60 s; the worktree is kept: {workdir}"]
    # A second resolution that raced the first (both read the attempt while it was
    # quarantined) reads the receipt and says nothing more.
    daemon._resolve_quarantine(stale, protocol.KillArgs(job_id, confirm_dead=True))
    assert calls == [1] and len(job_notices(daemon, job_id)) == 2


def test_c13_1_a_quarantine_release_says_what_its_snapshot_left_out(state_daemon, monkeypatch):
    """Real git: the release's salvage writes a ref and leaves out a nested repository with no
    commit; one more notice says so. A release with nothing to say writes none."""
    from subfleet import protocol
    from subfleet.procs import Containment
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id, attempt, _ = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    (workdir / "tracked.txt").write_text("provider progress\n")
    empty_repository(workdir / "newpkg")
    daemon._quarantine(attempt, Containment(marker_pids=frozenset({42099})), "escaped fixture")
    monkeypatch.setattr(daemon, "_contain", lambda attempt: Containment())
    daemon._resolve_quarantine(daemon.store.get_attempt(attempt["attempt_id"]),
                               protocol.KillArgs(job_id, confirm_dead=True))
    [artifact] = [r for r in daemon.store.list_artifacts(attempt["attempt_id"]) if r["role"] == "salvage"]
    assert git(workdir, "show", f"{artifact['path']}:tracked.txt") == "provider progress"
    evidence = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["salvage_skipped"] == {"count": 1, "paths": ["newpkg/"]} and "salvage_error" not in evidence
    _, released = job_notices(daemon, job_id)
    assert released.split("\n")[1:] == [
        "released from quarantine (attempt a1)",
        "salvage left out 1 nested repository with no commit, kept only in the worktree: 'newpkg/'; "
        f"the worktree is kept: {workdir}"]

    job2, attempt2, _ = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    daemon._quarantine(attempt2, Containment(marker_pids=frozenset({42099})), "escaped fixture")
    (workdir / "newpkg" / ".git").rename(workdir / "newpkg" / "was-a-repository")
    daemon._resolve_quarantine(daemon.store.get_attempt(attempt2["attempt_id"]),
                               protocol.KillArgs(job2, confirm_dead=True))
    assert len(job_notices(daemon, job2)) == 1                                # the quarantine's own


# --- a retry after a failed salvage (review of cda4c161, N2) ---------------------------


class TransientAdapter(FakeAdapter):
    def classify(self, attempt_dir, launch, exit_info):
        return Outcome(OutcomeClass.TRANSIENT, "fixture transport disconnected")


def retried(daemon, harness, monkeypatch, *, salvage_fails=True):
    """a1 wrote work and ended transient, so the job waits to try again; its salvage failed
    (timed out `SALVAGE_TRIES` times) or not. Returns what a2's admission needs."""
    workdir, job_id, attempt, _ = finalizing(daemon, harness)
    (workdir / "new-by-a1.txt").write_text("a1 work\n")
    register("codex", TransientAdapter)
    if salvage_fails:
        def slow(*args, **kwargs):
            raise SalvageError("git add timed out after 60 s", transient=True)
        monkeypatch.setattr(daemon_module, "salvage", slow)
        for _ in range(1, SALVAGE_TRIES):
            with pytest.raises(SalvageError):
                daemon._finalize(attempt)
    daemon._finalize(attempt)
    monkeypatch.setattr(daemon_module, "salvage", salvage_module.salvage)
    assert daemon.store.get_job(job_id)["state"] == "waiting"
    assert ("salvage_error" in json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])) \
        == salvage_fails
    daemon.store.update_job(job_id, next_check_at=None)
    return workdir, job_id


def test_c13_1_a_retry_after_a_failed_salvage_starts_from_a_held_snapshot(state_daemon, monkeypatch):
    """a1's work is then only a2's start snapshot, a tree object no ref held, which `gc` may
    prune and a2 goes on to edit, with nobody told. Admission holds it under
    `refs/subfleet-salvage/<job id>-a2-baseline` (a commit on a2's HEAD), names it in a2's
    evidence and records it as a2's salvage artifact; HEAD, the index and the files are
    untouched."""
    daemon, harness = state_daemon
    workdir, job_id = retried(daemon, harness, monkeypatch)
    status = git(workdir, "status", "--porcelain")
    head = git(workdir, "rev-parse", "HEAD")
    daemon._admit()
    a1, a2 = daemon.store.list_attempts(job_id)
    assert (a2["seq"], a2["state"]) == (2, "reserved")
    ref = f"refs/subfleet-salvage/{job_id}-a2-baseline"
    evidence = json.loads(a2["evidence_json"])
    assert evidence["baseline_ref"] == ref and evidence["baseline_commit"] == head
    [artifact] = daemon.store.list_artifacts(a2["attempt_id"])
    assert (artifact["role"], artifact["path"]) == ("salvage", ref)
    assert git(workdir, "rev-parse", f"{ref}^{{tree}}") == a2["baseline_tree"]
    assert git(workdir, "rev-parse", f"{ref}^") == head
    assert git(workdir, "show", f"{ref}:new-by-a1.txt") == "a1 work"
    assert git(workdir, "show", f"{ref}:tracked.txt") == "provider progress"
    assert git(workdir, "status", "--porcelain") == status and git(workdir, "rev-parse", "HEAD") == head
    assert git(workdir, "for-each-ref", "--format=%(refname)", "refs/subfleet-salvage/") == ref

    [held] = events(daemon, job_id, "salvage.baseline_held")
    assert held == {"ref": ref, "commit": git(workdir, "rev-parse", ref), "seq": 2, "after": a1["attempt_id"]}

    # A later admission pass (one rolled back, say) reuses the ref, whatever the checkout
    # holds by then: the first snapshot after the failure is the one that holds a1's work.
    job = daemon._job(job_id)
    again = daemon._pin_baseline(job, [a1], str(workdir), head, a2["baseline_tree"])
    assert again == {key: artifact[key] for key in ("role", "path", "sha256", "bytes")}
    other = git(workdir, "rev-parse", "HEAD^{tree}")
    assert daemon._pin_baseline(job, [a1], str(workdir), head, other) == again
    assert git(workdir, "rev-parse", f"{ref}^{{tree}}") == a2["baseline_tree"]
    assert git(workdir, "for-each-ref", "--format=%(refname)", "refs/subfleet-salvage/") == ref
    assert len(events(daemon, job_id, "salvage.baseline_held")) == 1


def test_c13_1_a_checkout_that_changes_while_the_retry_waits_keeps_the_first_held_snapshot(
        state_daemon, monkeypatch):
    """Review of 43b8bf29, F1: a retry is looked at on many admission passes before one
    places it, and an in-place job's checkout is the caller's, which may change in between.
    Each pass held its own snapshot: a new tree went beside the first, and that tree on a
    new HEAD (the caller committed what was there) collided with both, a permanent failure
    that failed the job. The first snapshot after the failed salvage already holds a1's work
    (what changed since is the caller's), so every later pass reuses it."""
    daemon, harness = state_daemon
    workdir, job_id = retried(daemon, harness, monkeypatch)
    ref = f"refs/subfleet-salvage/{job_id}-a2-baseline"
    head = git(workdir, "rev-parse", "HEAD")
    first = salvage_module.working_tree(workdir, head)
    # Exercise real admission passes: preparation happens before the capacity
    # decision. No new helper is required to reproduce the missing ref on r2.
    with monkeypatch.context() as held:
        held.setitem(daemon.policy["caps"], "max_active_attempts", 0)
        daemon._admit()
        assert daemon.store.get_job(job_id)["state"] == "waiting"
        assert len(daemon.store.list_attempts(job_id)) == 1
        assert git(workdir, "for-each-ref", "--format=%(refname)", "refs/subfleet-salvage/") == ref
        (workdir / "caller-edit.txt").write_text("the caller's own edit\n")
        daemon.store.update_job(job_id, next_check_at=None)
        daemon._admit()
        assert git(workdir, "for-each-ref", "--format=%(refname)", "refs/subfleet-salvage/") == ref
    git(workdir, "add", "-A")
    git(workdir, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-qm", "the caller's commit")
    daemon.store.update_job(job_id, next_check_at=None)
    daemon._admit()                                                                     # the pass that places it
    job = daemon.store.get_job(job_id)
    a1, a2 = daemon.store.list_attempts(job_id)
    assert (job["state"], a2["seq"], a2["state"]) == ("running", 2, "reserved")
    assert json.loads(a2["evidence_json"])["baseline_ref"] == ref
    assert [r["path"] for r in daemon.store.list_artifacts(a2["attempt_id"])] == [ref]
    assert git(workdir, "for-each-ref", "--format=%(refname)", "refs/subfleet-salvage/") == ref
    assert git(workdir, "rev-parse", f"{ref}^{{tree}}") == first and git(workdir, "rev-parse", f"{ref}^") == head
    assert git(workdir, "show", f"{ref}:new-by-a1.txt") == "a1 work"
    [held] = events(daemon, job_id, "salvage.baseline_held")
    assert held["ref"] == ref


def test_c13_1_a_retry_with_no_commit_to_hold_a_failed_salvage_on_fails(state_daemon, monkeypatch):
    """Review of 43b8bf29, F3: a1's salvage failed, and by a2's admission the worktree's
    repository is gone (the thesis-* jobs' shape: every git call says `not a git
    repository`), so there is no HEAD and no start snapshot to hold a1's work in. a2 was
    reserved and ran there unheld; now the job fails with the cause, as any baseline that
    cannot be held does (C-6.8), and a1's files are left as they are."""
    daemon, harness = state_daemon
    workdir, job_id = retried(daemon, harness, monkeypatch)
    (workdir / ".git").rename(workdir.parent / "moved-away.git")
    (workdir / ".git").write_text(f"gitdir: {workdir.parent / 'deleted-repository' / '.git' / 'worktrees' / 'x'}\n")
    try:
        daemon._admit()
    finally:
        (workdir / ".git").unlink()
        (workdir.parent / "moved-away.git").rename(workdir / ".git")
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1) and len(daemon.store.list_attempts(job_id)) == 1
    assert (f"workspace preparation failed: SalvageError: attempt a1's salvage failed and {workdir} "
            "has no commit to hold its work on") in job_notices(daemon, job_id)[-1]
    assert (workdir / "new-by-a1.txt").read_text() == "a1 work\n"
    assert git(workdir, "for-each-ref", "--format=%(refname)", "refs/subfleet-salvage/") == ""


def test_c13_1_a_retry_after_a_salvage_that_succeeded_holds_nothing_more(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    workdir, job_id = retried(daemon, harness, monkeypatch, salvage_fails=False)
    daemon._admit()
    a1, a2 = daemon.store.list_attempts(job_id)
    assert a2["state"] == "reserved" and "baseline_ref" not in json.loads(a2["evidence_json"])
    assert daemon.store.list_artifacts(a2["attempt_id"]) == []
    [salvaged] = [r for r in daemon.store.list_artifacts(a1["attempt_id"]) if r["role"] == "salvage"]
    assert git(workdir, "for-each-ref", "--format=%(refname)", "refs/subfleet-salvage/") == salvaged["path"]


@pytest.mark.parametrize("transient", [True, False])
def test_c13_1_a_baseline_that_cannot_be_held_is_a_workspace_failure(state_daemon, monkeypatch, transient):
    """C-6.8: nothing runs in the worktree unheld; the job waits, or fails with the cause."""
    daemon, harness = state_daemon
    workdir, job_id = retried(daemon, harness, monkeypatch)

    def refuse(*args, **kwargs):
        raise SalvageError("git update-ref failed: fatal: refused", transient=transient)
    monkeypatch.setattr(daemon_module, "pin_baseline", refuse)
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert len(daemon.store.list_attempts(job_id)) == 1
    if transient:
        assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
    else:
        assert (job["state"], job["rc"]) == ("failed", 1)
        assert "workspace preparation failed: SalvageError: git update-ref failed" in job_notices(daemon, job_id)[0]
    assert (workdir / "new-by-a1.txt").read_text() == "a1 work\n"


# --- the causes in the live daemon.log (2026-09-28) ------------------------------------
#
# The installed daemon (b053e3de) re-raised every salvage failure, so each of these held
# its attempt in `finalizing`, and its lane, for as long as it lasted. Each was reproduced
# read-only against the stuck attempt's own worktree (new objects and the index in /tmp);
# each test below is that worktree's shape, with real git.


@ROOT
def test_c13_1_live_a_file_git_cannot_read_is_recorded_at_once(state_daemon):
    """r218-conv-opus/a2 (live, `finalizing` for hours, 128 tries): a reviewer's fixture left
    a mode-000 file under untracked scratch, `error: open(".review-scratch/…/stdin.jsonl"):
    Permission denied`. No retry reads it, so it is recorded on the first try."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    locked = workdir / ".review-scratch/r2/adopt/tmp/a1-mode000-adopt-x/state/jobs/turn-job-0/a1/stdin.jsonl"
    locked.parent.mkdir(parents=True)
    locked.write_text("{}\n")
    locked.chmod(0)
    try:
        daemon._finalize(attempt)
    finally:
        locked.chmod(0o600)
    error = json.loads((adir / "salvage.json").read_text())["error"]
    assert error.startswith("salvage failed: git add failed: error: open(") and "Permission denied" in error
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []
    assert (workdir / "tracked.txt").read_text() == "provider progress\n"


def test_c13_1_live_an_empty_repository_beside_a_committed_one_is_left_out(state_daemon):
    """c68r2-a/a1 and receipt-spent-budget-r1/a1 (live): `.pt/elsewhere0` (a repository with a
    commit, which git adds as a gitlink) beside `.pt/elsewhere1/`, which `does not have a
    commit checked out`, and one inside an untracked virtualenv. Only the empty ones are
    left out; the committed one is in the snapshot."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    committed_repository(workdir / ".pt" / "elsewhere0")
    empty_repository(workdir / ".pt" / "elsewhere1")
    empty_repository(workdir / "scratch/bt/.venv312/snapshot-acceptance0/repository")
    daemon._finalize(attempt)
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt["error"] is None
    assert sorted(receipt["skipped"]) == [".pt/elsewhere1/", "scratch/bt/.venv312/snapshot-acceptance0/repository/"]
    ref = receipt["result"]["ref"]
    assert git(workdir, "ls-tree", ref, ".pt/").split()[:2] == ["160000", "commit"]    # the gitlink
    assert git(workdir, "show", f"{ref}:tracked.txt") == "provider progress"
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []


def test_c13_1_live_a_worktree_whose_repository_was_deleted_is_recorded_at_once(state_daemon):
    """thesis-prepush-guard-r21/a1 and thesis-resolver-exit3-merge-review/a1 (live): the
    repository the worktree was cut from was deleted, so every git call in it says `not a
    git repository`. Recorded on the first try; the files are left alone."""
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    (workdir / ".git").rename(workdir.parent / "moved-away.git")
    (workdir / ".git").write_text(f"gitdir: {workdir.parent / 'deleted-repository' / '.git' / 'worktrees' / 'x'}\n")
    try:
        daemon._finalize(attempt)
    finally:
        (workdir / ".git").unlink()
        (workdir.parent / "moved-away.git").rename(workdir / ".git")
    error = json.loads((adir / "salvage.json").read_text())["error"]
    assert error.startswith("salvage failed: git rev-parse failed: fatal: not a git repository")
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []
    assert (workdir / "tracked.txt").read_text() == "provider progress\n"


def test_c13_1_live_git_past_its_cap_is_tried_again_then_recorded(state_daemon, tmp_path, monkeypatch):
    """receipt-crash-refusal-r1b/a2, rac-review-astra/a1, the r218 jobs and the microcosm and
    policyengine-us jobs (live): `add -A` over thousands of untracked scratch files, or a large
    checkout, at a load average of 200 to 350, ran past `workspace_git_timeout_s` (60 s) on
    every try. Here `git … add` is a stand-in that sleeps past a 1 s cap (review of
    43b8bf29, F5: no real `git add` runs; what is real is the cap, which kills it, and every
    other git call): tried `SALVAGE_TRIES` times, then recorded, and the attempt ends and
    frees its lane."""
    import shutil
    import stat
    daemon, harness = state_daemon
    workdir, job_id, attempt, adir = finalizing(daemon, harness)
    bindir = tmp_path / "slow-add-bin"
    bindir.mkdir()
    script = bindir / "git"
    # `git -C <dir> add …` sleeps past the cap (`exec`: the sleep is what the cap kills).
    script.write_text(f'#!/bin/sh\nif [ "$3" = "add" ]; then exec sleep 30; fi\nexec "{shutil.which("git")}" "$@"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    add = salvage_module._add

    def capped_add(workdir, env, pathspec, timeout_s):
        # Only the sleeping add has the short cap. Real setup/probe calls retain
        # their normal cap, so load cannot turn this into a read-tree reproduction.
        return add(workdir, env, pathspec, 1)

    monkeypatch.setattr(salvage_module, "_add", capped_add)
    for tries in range(1, SALVAGE_TRIES):
        with pytest.raises(SalvageError, match="git add timed out after 1 s") as caught:
            daemon._finalize(attempt)
        assert caught.value.transient and not (adir / "salvage.json").exists()
        assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "finalizing"
    daemon._finalize(attempt)
    assert json.loads((adir / "salvage.json").read_text())["error"] == "salvage failed: git add timed out after 1 s"
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and lane_leases(daemon) == []
