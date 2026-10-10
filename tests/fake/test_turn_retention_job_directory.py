"""Review 3 regression: a live turn keeps retirement's job directory."""
from pathlib import Path

from subfleet import folders, retention
from subfleet import retention_archive as rarch
from tests.fake.test_admission_latency import fleet_daemon, measure
from tests.fake.test_admission_liveness import CODEX, _checkout, _end, _live
from tests.fake.test_turn_wait_reasons import SETTINGS, message_in, reason
from tests.unit.test_retention_shared_folders import retiring_tree


def test_review_retirement_leaves_a_live_readonly_turns_job_directory_in_place(tmp_path):
    """A permitted turn keeps the job directory that retirement also moves."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        retiring_tree(daemon, harness)
        job_folder = folders.canonical(daemon.root / "jobs" / "retired")
        options = {**SETTINGS, "permission": "read-only"}
        assert daemon.conversations._validate_workspace(
            "codex", job_folder, options, kind="in-place", allow_main=False) == job_folder
        _, _, turn = message_in(daemon, harness, "Job directory", workspace=job_folder, settings=options)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        daemon._admit_turns()
        assert _live(daemon, turn)
        assert folders.turn_holds(daemon.store.query, job_folder, (folders.READER,))
        moved_while_live = []
        quarantine = rarch.Retirement.quarantine

        def observe(retirement):
            result = quarantine(retirement)
            if retirement.job_id == "retired":
                moved_while_live.append((_live(daemon, turn), Path(job_folder).is_dir()))
            return result

        patch.setattr(rarch.Retirement, "quarantine", observe)
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        assert not any(live and not cwd_exists for live, cwd_exists in moved_while_live), {
            "observed_live_and_folder_exists": moved_while_live, "result": result}
        assert "retired" in result["protected"] and "retired" not in result["pruned"], result
        assert Path(job_folder).is_dir()
        assert daemon.store.get_job("retired")
        _end(daemon, turn)
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        assert "retired" in result["pruned"], result
        assert not Path(job_folder).exists()


def test_a_turn_submitted_after_the_job_directory_fence_waits_and_keeps_its_cwd(tmp_path):
    """The post-fence waiter pins the archive commit, then starts after rollback."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        retiring_tree(daemon, harness)
        job_folder = folders.canonical(daemon.root / "jobs" / "retired")
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        fence = rarch.Retirement._fence_job_folder
        seen = {}

        def submit_after_fence(retirement):
            result = fence(retirement)
            if retirement.job_id == "retired" and not seen:
                _, mid, turn = message_in(daemon, harness, "After job fence", workspace=job_folder,
                                         settings={**SETTINGS, "permission": "read-only"})
                daemon._admit_turns()
                seen.update(turn=turn, reason=reason(daemon, mid))
                assert not _live(daemon, turn), daemon._holds
                assert daemon._holds[turn]["leases"] == [folders.exclusive_key(job_folder)]
                assert daemon._holds[turn]["folder"] == job_folder
                assert job_folder in seen["reason"] and "job directory" in seen["reason"]
            return result

        patch.setattr(rarch.Retirement, "_fence_job_folder", submit_after_fence)
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        assert "retired" in result["protected"] and "retired" not in result["pruned"], result
        assert Path(job_folder).is_dir()
        assert not daemon.store.query("SELECT 1 FROM leases WHERE holder='retention:retired'")
        daemon._admit_turns()
        assert _live(daemon, seen["turn"]), daemon._holds
