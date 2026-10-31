"""Executed input-head comparisons; all Git fixtures belong to pytest's TMPDIR."""
import json
from pathlib import Path
import types

import pytest

from subfleet import dependencies
from tests.unit.test_retention_location_cycles import ROWS, cycle_route
from tests.unit.test_retention_locations import routine_input

DATA = Path(__file__).resolve().parent
BASE = types.ModuleType("subfleet.r10_base")
BASE.__package__ = "subfleet"
exec(compile((DATA / "base-dependencies.py.txt").read_text(), "475f6327890e:subfleet/dependencies.py", "exec"), BASE.__dict__)


@pytest.mark.parametrize("route,cycle,git_exit,error_count", ROWS)
def test_baseline_route_files(tmp_path, route, cycle, git_exit, error_count, record_property):
    checkout = cycle_route(tmp_path, route, cycle)
    before = BASE.record(str(checkout))
    record_property("baseline-route", json.dumps(dict(route=route, cycle=cycle,
                                                      recorded=before), sort_keys=True))
    assert len(before.get("incomplete", [])) == error_count


@pytest.mark.parametrize("layout", ["plain", "worktree", "shared", "new-workdir", "none"])
def test_five_routine_controls_against_475f6327890e(tmp_path, layout, record_property):
    checkout, expected = routine_input(tmp_path, layout)
    before = BASE.record(str(checkout))
    after = dependencies.record(str(checkout))
    assert before == after == expected
    record_property("baseline-routine", json.dumps(dict(layout=layout, before=before, after=after), sort_keys=True))


@pytest.mark.parametrize("route,cycle,git_exit,error_count", ROWS)
def test_route_comparison_against_475f6327890e(tmp_path, route, cycle, git_exit,
                                            error_count, record_property):
    checkout = cycle_route(tmp_path, route, cycle)
    stable = tmp_path.resolve() / "stable"
    from tests.unit.test_retention_location_inputs import repo
    repo(stable)
    baseline = BASE.record(str(checkout), review_root=str(stable))
    actual = dependencies.record(str(checkout), review_root=str(stable))
    assert baseline.get("incomplete", []) == actual.get("incomplete", [])
    assert set(actual.get("git", [])) <= set(baseline.get("git", []))
    dropped = sorted(set(baseline.get("git", [])) - set(actual.get("git", [])))
    import errno
    import os
    for place in dropped:
        with pytest.raises(OSError) as failure:
            os.stat(place)
        assert failure.value.errno == errno.ELOOP
    # Remove the cycle route entirely, on this identical fixture. Healthy storage
    # is compared with the old reader on precisely that input, not a new layout.
    (tmp_path.resolve() / "loop").unlink()
    healthy_before = BASE.git_storage(str(stable))
    healthy_after = dependencies.git_storage(str(stable))
    assert healthy_before == healthy_after
    assert set(healthy_before) <= set(actual.get("git", []))
    from subfleet import folders
    def unspelled(places):
        # The returned physical entries in these fixtures have stable parents;
        # the only non-fixed-point strings are inside the stopped cycles.
        import os
        return [p for p in places if os.path.join(folders.canonical(os.path.dirname(p)),
                                                 os.path.basename(p)) != p]
    # Restore the link for the exact return-equation audit.
    (tmp_path.resolve() / "loop").symlink_to("loop/tail" if cycle == "self-suffix" else "other/tail")
    record_property("route-comparison", json.dumps(dict(route=route, cycle=cycle,
        baseline=baseline, actual=actual, baseline_unspelled=unspelled(baseline.get("git", [])),
        actual_unspelled=unspelled(actual.get("git", [])), healthy_before=healthy_before,
        healthy_after=healthy_after, dropped_unreadable=dropped), sort_keys=True))
