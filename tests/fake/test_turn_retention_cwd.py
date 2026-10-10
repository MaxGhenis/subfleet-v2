"""Turn cwd safety when its Git hold differs, and in retirement's job directory.

Ported from review 3 of PR #138. No provider or daemon process is launched.
"""
from pathlib import Path

import pytest

from subfleet import folders, retention
from subfleet import retention_archive as rarch
from subfleet.salvage import git_toplevel
from tests.fake.test_admission_latency import fleet_daemon, measure
from tests.fake.test_admission_liveness import CODEX, _checkout, _live
from tests.fake.test_turn_wait_reasons import SETTINGS, message_in, reason
from tests.unit.retention_world import git
from tests.unit.test_retention_shared_folders import retiring_tree


@pytest.mark.parametrize("alias", [False, True], ids=["canonical", "case-alias"])
@pytest.mark.parametrize("after_quarantine", [False, True], ids=["before-move", "after-move"])
def test_review_readonly_turn_with_external_core_worktree_waits_for_its_cwd_fence(tmp_path, alias, after_quarantine):
    """Persisted core.worktree changes the Git hold, but leaves the provider cwd.

    Real validation, submission, workspace preparation and quarantine run.
    Submission follows selection, while retention already holds its fence.
    """
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        typed = nested.replace("/worktrees/retired", "/worktrees/rETIRED", 1) if alias else nested
        if not Path(typed).exists() or not Path(typed).samefile(nested):
            pytest.skip("requires a case-insensitive volume")
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

        def admit_and_record():
            turn = seen["turn"]
            daemon._admit_turns()
            seen["reserved_under_fence"] = _live(daemon, turn)
            seen["rows_in_tree"] = folders.turn_holds(daemon.store.query, tree, inside=True)
            seen["hold"] = daemon._holds.get(turn)
            seen["reason"] = reason(daemon, seen["mid"])

        def submit_after_selection(retirement, job, pool):
            result = begin(retirement, job, pool)
            if retirement.job_id == "retired" and "turn" not in seen:
                assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?",
                                        (folders.exclusive_key(tree),))["holder"] == "retention:retired"
                _, mid, turn = message_in(daemon, harness, "External git worktree", workspace=typed,
                                         settings=options)
                seen["turn"], seen["mid"] = turn, mid
                seen["recorded_folder"] = daemon._submitted(turn).get("folder")
                seen["run_folder"] = daemon._job(turn)["workdir"]
                assert seen["recorded_folder"] == external_folder
                assert folders.canonical(seen["run_folder"]) == nested
                if not after_quarantine:
                    admit_and_record()
            return result

        def observe_move(retirement):
            result = quarantine(retirement)
            if retirement.job_id == "retired":
                if after_quarantine:
                    assert not Path(typed).exists()
                    assert Path(external_folder).is_dir()
                    admit_and_record()
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
        expected = typed if after_quarantine else nested
        assert seen["hold"]["folder"] == expected
        assert tree in seen["reason"]
        assert Path(nested).is_dir()
        daemon._admit_turns()
        assert _live(daemon, seen["turn"]), daemon._holds


@pytest.mark.parametrize("alias", [False, True], ids=["canonical", "case-alias"])
def test_a_missing_actual_cwd_waits_for_workspace_even_when_its_git_hold_exists(tmp_path, alias):
    """No retirement fence: an existing Git hold cannot authorize a missing cwd."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        typed = nested.replace("/worktrees/retired", "/worktrees/rETIRED", 1) if alias else nested
        if not Path(typed).exists() or not Path(typed).samefile(nested):
            pytest.skip("requires a case-insensitive volume")
        external = tmp_path / "external-worktree"
        external.mkdir()
        git(Path(nested), "config", "core.worktree", str(external))
        _, _, turn = message_in(daemon, harness, "Missing cwd", workspace=typed,
                                 settings={**SETTINGS, "permission": "read-only"})
        assert daemon._submitted(turn)["folder"] == folders.canonical(external)
        aside = Path(tree).with_name(".retired.aside")
        Path(tree).rename(aside)
        daemon._admit_turns()
        assert not _live(daemon, turn)
        assert not [row for row in daemon.store.list_leases() if row["holder"] == turn]
        assert daemon._holds[turn]["reason"] == "workspace"
        assert daemon._holds[turn]["error_type"] == "FileNotFoundError"
        aside.rename(tree)
        daemon.store.update_job(turn, next_check_at=None)
        daemon._admit_turns()
        assert _live(daemon, turn), daemon._holds
        assert {row["lease_key"] for row in daemon.store.list_leases()
                if row["holder"] == turn and folders.parse(row["lease_key"])} == {
                    folders.turn_key(folders.canonical(external), turn, writable=False),
                    folders.turn_key(nested, turn, writable=False)}
