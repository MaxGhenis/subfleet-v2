"""Run the complete real-Git controller shape on head independently of the base."""
from __future__ import annotations

from subfleet import daemon as daemon_module
from tests.fake.test_admission_tree_thrash import (fleet_factory, immutable_source,
                                                   measured_shape)  # noqa: F401
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_unrun_worktrees import repo  # noqa: F401
from tests.unit.test_salvage import git


def test_head_twenty_two_start_rounds_cut_forty_trees_and_give_none_back(
        fleet_factory, repo, immutable_source, monkeypatch):  # noqa: F811
    """The controller closes after reservation; its labelled closure-race term is zero.

    As in the differential measured test, immutable source snapshots are cached
    while every worktree add, registration and safety sweep remains real Git.
    """
    head = git(repo, "rev-parse", "HEAD").strip()
    baseline = git(repo, "rev-parse", "HEAD^{tree}").strip()
    with monkeypatch.context() as fixed_clean_source:
        fixed_clean_source.setattr(daemon_module, "git_head", lambda *args, **kwargs: head)
        fixed_clean_source.setattr(daemon_module, "working_tree", lambda *args, **kwargs: baseline)
        result = measured_shape(fleet_factory, repo, base=False)
    assert result == {
        "cuts": 40, "reservations": 40, "give_backs": 0, "closure_race_cuts": 0,
        "trace": [(index, "codex-1") for index in range(40)],
    }
