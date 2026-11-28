"""A broad terminal offer must fail the named lease-publication regression."""

import pytest

from subfleet import daemon as daemon_module
from tests.unit.test_census_round2 import (
    daemon, test_cwd_worker_never_offers_or_releases_a_job_with_other_leases as other_leases,
    JOB,
)  # noqa: F401


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="offering every terminal worktree lease must fail other_leases")
def test_round2_broad_terminal_offer_fails_the_other_leases_regression(daemon, monkeypatch):
    monkeypatch.setattr(daemon_module, "CWD_WORKTREE_HOLDS",
        "SELECT DISTINCT jobs.job_id FROM jobs JOIN leases l ON l.holder=jobs.job_id "
        "WHERE jobs.state IN ('succeeded','failed','cancelled','lost') "
        "AND l.lease_key LIKE 'worktree:%' ")
    other_leases(daemon, JOB, "push:pending")
