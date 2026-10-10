"""Review 4: a live turn keeps its cwd inside retention's quarantine."""
from pathlib import Path

import pytest

from subfleet import folders, retention
from subfleet import retention_archive as rarch
from tests.fake.test_admission_latency import fleet_daemon, measure
from tests.fake.test_admission_liveness import CODEX, _checkout, _end, _live
from tests.fake.test_turn_wait_reasons import SETTINGS, message_in
from tests.unit.test_retention_shared_folders import retiring_tree


@pytest.mark.parametrize("ending", ["commit", "holder-rollback"])
@pytest.mark.parametrize("location", ["nested-worktree", "worktree", "job-directory"])
def test_t2_readonly_turn_in_quarantine_keeps_its_live_cwd(tmp_path, ending, location):
    """Normal submission/admission in a quarantined nested repo, then reclaim.

    Ported from review 4's commit and holder-rollback variants, with exact
    tree and job-directory paths, repeated recovery, and terminal release.
    """
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        relative = Path(nested).relative_to(tree)
        seen = {}
        quarantine = rarch.Retirement.quarantine

        def after_quarantine(retirement):
            result = quarantine(retirement)
            if retirement.job_id == "retired" and not seen:
                actual = {"nested-worktree": retirement.q_worktree / relative,
                          "worktree": retirement.q_worktree,
                          "job-directory": retirement.q_job}[location]
                assert actual.is_dir()
                _, _, turn = message_in(daemon, harness, "Live in quarantine", workspace=actual,
                                       settings={**SETTINGS, "permission": "read-only"})
                seen.update(turn=turn, actual=actual)
                assert folders.canonical(daemon._job(turn)["workdir"]) == str(actual)
                daemon._admit_turns()
                assert _live(daemon, turn), daemon._holds
                assert folders.turn_holds(daemon.store.query, folders.canonical(actual), (folders.READER,))
            return result

        patch.setattr(rarch.Retirement, "quarantine", after_quarantine)

        def holders(watches, **options):
            # A holder scan can trigger rollback before the archive exists.
            return {"retired": ["review: cwd holder"]} if ending == "holder-rollback" and seen else {}

        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=holders)
        assert seen and _live(daemon, seen["turn"]), seen
        assert seen["actual"].is_dir(), {"live_cwd_removed": seen, "retention": result}
        journal = rarch.load_journal(daemon.root, "retired")
        assert journal is not None
        assert bool(journal.get("rollback_pending")) == (ending == "holder-rollback")
        # A fresh retention state replays durable recovery while the turn lives.
        for _ in range(2):
            retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0, holders=holders)
            assert _live(daemon, seen["turn"]) and seen["actual"].is_dir()
            assert rarch.load_journal(daemon.root, "retired") is not None
        _end(daemon, seen["turn"])
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        assert not seen["actual"].exists(), result
        assert not daemon.store.list_leases("retention:retired")
        if ending == "holder-rollback":
            assert Path(tree).is_dir() and daemon.store.get_job("retired"), result
            assert (daemon.root / "jobs" / "retired").is_dir()
        else:
            assert "retired" in result["reclaimed"] and not daemon.store.get_job("retired"), result
            assert rarch.load_journal(daemon.root, "retired") is None


@pytest.mark.parametrize("ending", ["commit", "holder-rollback"])
def test_turn_arriving_after_quarantine_cleanup_fence_waits(tmp_path, ending):
    """A submission between the final guard and cleanup cannot become live."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        relative = Path(nested).relative_to(tree)
        seen = {}
        fence = rarch.Retirement._fence_quarantine

        def submit_after_fence(retirement):
            result = fence(retirement)
            if retirement.job_id == "retired" and not seen:
                assert result is None
                actual = retirement.q_worktree / relative
                assert actual.is_dir()
                _, _, turn = message_in(daemon, harness, "After cleanup fence", workspace=actual,
                                       settings={**SETTINGS, "permission": "read-only"})
                seen["turn"] = turn
                daemon._admit_turns()
                assert not _live(daemon, turn), daemon._holds
                assert daemon._holds[turn]["leases"] == [folders.exclusive_key(str(retirement.work))]
            return result

        patch.setattr(rarch.Retirement, "_fence_quarantine", submit_after_fence)
        holders = lambda watches, **_: {"retired": ["review: rollback"]} if ending == "holder-rollback" else {}
        retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0, holders=holders)
        assert seen and not _live(daemon, seen["turn"])
        assert not daemon.store.list_leases("retention:retired")
