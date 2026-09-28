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

import pytest

from subfleet import daemon as daemon_module
from subfleet import salvage as salvage_module
from subfleet.daemon import SALVAGE_SKIPPED_SHOWN, SALVAGE_TRIES
from subfleet.salvage import SalvageError, SalvageResult
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401 (a fixture)
from tests.fake.test_workspace_contract import repository
from tests.unit.test_salvage import git
from tests.unit.test_salvage_unindexable import fake_add, on_a_branch_whose_name_is_not_utf8

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
