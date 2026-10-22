"""Strict mutation controls: each mutant must fail its named regression."""

import inspect
import textwrap

import pytest

from subfleet import procs
from subfleet.daemon import Daemon
from tests.fake.test_census_cwd_outcome import (
    core, test_cwd_only_preserves_outcome_and_holds_only_worktree as cwd_outcome,
)  # noqa: F401
from tests.unit.test_census_counted_expiry import (
    test_claude_shaped_command_groups_reach_a_steady_record_size as bounded,
    test_counted_expires_only_after_two_distinct_absent_tables as two_tables,
)
from tests.unit.test_procs_cwd import (
    test_cwd_comparison_uses_disk_case_on_case_insensitive_apfs as case_spelling,
)

pytestmark = pytest.mark.real_cwd


def replace_function(monkeypatch, owner, name, replacements):
    original = getattr(owner, name)
    source = textwrap.dedent(inspect.getsource(original))
    for before, after in replacements:
        assert before in source
        source = source.replace(before, after)
    namespace = {}
    exec(compile(source, f"<mutation:{name}>", "exec"), original.__globals__, namespace)
    monkeypatch.setattr(owner, name, namespace[name])


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the named regression must detect this mutant")
@pytest.mark.parametrize("mutant", ["cwd-quarantines", "never-expire", "one-miss", "raw-paths"])
def test_round1_mutant_fails_named_regression(mutant, core, monkeypatch, tmp_path):
    if mutant == "cwd-quarantines":
        replace_function(monkeypatch, Daemon, "_finalize", [
            ("if not census.containment_empty:", "if not census.verified_empty:")])
        cwd_outcome(core, monkeypatch, "receipt")
    elif mutant == "never-expire":
        replace_function(monkeypatch, Daemon, "_record_owned", [
            ("dropping = expired if key == 'counted_identities' else {}", "dropping = {}")])
        bounded()
    elif mutant == "one-miss":
        replace_function(monkeypatch, Daemon, "_record_owned", [
            ("if previous.get(pid_text) == known:", "if True:")])
        two_tables("absent")
    else:
        replace_function(monkeypatch, procs, "worktree_cwd_pids", [
            ("canonical(worktree)", "os.path.realpath(worktree)"),
            ("canonical(cwd)", "os.path.realpath(cwd)")])
        case_spelling(tmp_path, monkeypatch)
