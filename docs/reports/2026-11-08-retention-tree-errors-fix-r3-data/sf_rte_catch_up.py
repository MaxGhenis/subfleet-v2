"""Give just the six reproduced CI cases the existing catch-up fixture."""

import pytest


WALK = "tests/unit/test_rte_walk_bundle.py"
BUNDLE = "test_the_bundle_step_succeeds_whatever_an_earlier_attempt_left"
YOUNG = "test_a_bundle_lock_under_ten_minutes_old_defers_and_changes_nothing"
RECLAIM = (
    "tests/unit/test_rte_reclaim_acl.py::"
    "test_a_retired_job_s_deny_delete_file_ends_in_retention_conflicts_and_the_pass_finishes"
)
NODEIDS = {
    *(f"{WALK}::{BUNDLE}[{left}]" for left in ("neither", "tmp", "lock", "both")),
    f"{WALK}::{YOUNG}",
    RECLAIM,
}


def pytest_collection_modifyitems(items):
    marked = set()
    for item in items:
        if item.nodeid in NODEIDS:
            item.add_marker(pytest.mark.retention_catch_up)
            marked.add(item.nodeid)
    assert marked == NODEIDS
