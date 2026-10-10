"""Turn cwd safety when its Git hold differs, and in retirement's job directory.

Ported from review 3 of PR #138. No provider or daemon process is launched.
"""
from pathlib import Path

from subfleet import folders, retention
from subfleet import retention_archive as rarch
from subfleet.salvage import git_toplevel
from tests.fake.test_admission_latency import fleet_daemon, measure
from tests.fake.test_admission_liveness import CODEX, _checkout, _live
from tests.fake.test_turn_wait_reasons import SETTINGS, message_in, reason
from tests.unit.retention_world import git
from tests.unit.test_retention_shared_folders import retiring_tree


def test_review_readonly_turn_with_external_core_worktree_waits_for_its_cwd_fence(tmp_path):
    """Persisted core.worktree changes the Git hold, but leaves the provider cwd.

    Real validation, submission, workspace preparation and quarantine run.
    Submission follows selection, while retention already holds its fence.
    """
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        external = tmp_path / "external-worktree"
        external.mkdir()
        (external / "f.txt").write_text("nested\n")
        external_folder = folders.canonical(external)
        git(Path(nested), "config", "core.worktree", str(external))
        assert folders.canonical(git_toplevel(nested)) == external_folder
        assert folders.within(nested, tree) and not folders.within(external_folder, tree)
        options = {**SETTINGS, "permission": "read-only"}
        assert daemon.conversations._validate_workspace(
            "codex", nested, options, kind="in-place", allow_main=False) == nested
        seen = {"moves": []}
        begin, quarantine = rarch.Retirement.begin, rarch.Retirement.quarantine

        def submit_after_selection(retirement, job, pool):
            result = begin(retirement, job, pool)
            if retirement.job_id == "retired" and "turn" not in seen:
                assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?",
                                        (folders.exclusive_key(tree),))["holder"] == "retention:retired"
                _, mid, turn = message_in(daemon, harness, "External git worktree", workspace=nested,
                                         settings=options)
                seen["turn"] = turn
                seen["recorded_folder"] = daemon._submitted(turn).get("folder")
                seen["run_folder"] = daemon._job(turn)["workdir"]
                assert seen["recorded_folder"] == external_folder
                assert folders.canonical(seen["run_folder"]) == nested
                daemon._admit_turns()
                seen["reserved_under_fence"] = _live(daemon, turn)
                seen["rows_in_tree"] = folders.turn_holds(daemon.store.query, tree, inside=True)
                seen["hold"] = daemon._holds.get(turn)
                seen["reason"] = reason(daemon, mid)
            return result

        def observe_move(retirement):
            result = quarantine(retirement)
            if retirement.job_id == "retired":
                seen["moves"].append((_live(daemon, seen["turn"]), Path(nested).is_dir()))
            return result

        patch.setattr(rarch.Retirement, "begin", submit_after_selection)
        patch.setattr(rarch.Retirement, "quarantine", observe_move)
        seen["result"] = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                               holders=lambda watches, **_: {})
        assert not seen["reserved_under_fence"], seen
        assert not any(live and not cwd_exists for live, cwd_exists in seen["moves"]), seen
        assert seen["rows_in_tree"] == []
        assert seen["hold"]["leases"] == [folders.exclusive_key(tree)]
        assert seen["hold"]["folder"] == nested
        assert tree in seen["reason"]
        assert Path(nested).is_dir()
        daemon._admit_turns()
        assert _live(daemon, seen["turn"]), daemon._holds
