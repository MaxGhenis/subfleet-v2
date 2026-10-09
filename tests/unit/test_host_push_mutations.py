"""Exercise each requested safety mutation against the integration invariants.

Mutations live only in monkeypatched memory. Each runs the same real submit,
acceptance and local push invariant, and must cause its assertion to fail.
"""
import subprocess

import pytest

from subfleet import host_push
from test_host_push import (
    test_never_executes_job_controlled_git_config_or_hooks as config_invariant,
    test_never_pushes_a_tip_not_descending_from_recorded_base as ancestry_invariant,
    test_never_pushes_github_changes_including_reverted_intermediate_commits as workflow_invariant,
    test_never_submits_or_pushes_a_protected_branch as protected_invariant,
    test_new_branch_push_is_non_forcing_non_deleting_and_reexport_is_idempotent as refspec_invariant,
    worlds,
)


def test_mutation_drop_protected_check_is_caught(worlds, monkeypatch):
    monkeypatch.setattr(host_push, "check_branch", lambda *args, **kwargs: None)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        protected_invariant.hypothesis.inner_test(worlds, "main")


def test_mutation_allow_forcing_refspec_is_caught(worlds, monkeypatch):
    monkeypatch.setattr(host_push, "push_refspec", lambda sha, branch: f"+{sha}:refs/heads/{branch}")
    with pytest.raises(AssertionError):
        refspec_invariant.hypothesis.inner_test(worlds, "mutated")


def test_mutation_skip_descends_from_base_is_caught(worlds, monkeypatch):
    actual = host_push.git
    def mutate(repo, *args, **kwargs):
        if args[:2] == ("merge-base", "--is-ancestor"):
            return b""
        return actual(repo, *args, **kwargs)
    monkeypatch.setattr(host_push, "git", mutate)
    with pytest.raises(AssertionError):
        ancestry_invariant.hypothesis.inner_test(worlds, "orphan")


def test_mutation_skip_github_check_is_caught(worlds, monkeypatch):
    actual = host_push.git
    def mutate(repo, *args, **kwargs):
        if args[0] == "diff-tree":
            return b""
        return actual(repo, *args, **kwargs)
    monkeypatch.setattr(host_push, "git", mutate)
    with pytest.raises(AssertionError):
        workflow_invariant.hypothesis.inner_test(worlds, ".github/workflows/ci.yml", False)


def test_mutation_push_in_job_worktree_is_caught_by_sentinel(worlds, monkeypatch):
    actual = host_push.git
    def mutate(repo, *args, **kwargs):
        if args[0] == "push":
            # The sole attacker payload here writes the test-owned sentinel.
            # Deliberately omit config overrides to model the forbidden intake.
            worktree = repo.parents[3] / "work"
            result = subprocess.run([host_push.GIT, "-C", str(worktree), *args],
                                    env=host_push._environment(), capture_output=True, timeout=60)
            assert result.returncode == 0, result.stderr
            return result.stdout
        return actual(repo, *args, **kwargs)
    monkeypatch.setattr(host_push, "git", mutate)
    with pytest.raises(AssertionError):
        config_invariant.hypothesis.inner_test(worlds, "core.hooksPath")
