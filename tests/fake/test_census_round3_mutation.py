"""Keeping the exclusive key again (9b423bd26's hold) must fail the named regression."""

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.daemon import Daemon
from tests.fake.test_census_round3_leases import (
    observed, test_r3_resume_is_admitted_while_a_finished_sources_observer_lives as resume_admitted,
)  # noqa: F401
from tests.fake.test_state_contract import state_daemon  # noqa: F401


@pytest.mark.xfail(strict=True, raises=AdapterError,
                   reason="a kept `worktree:` key refuses the resume at submit (worktree-lease)")
def test_r3_keeping_the_exclusive_key_fails_the_resume_regression(observed, monkeypatch):
    monkeypatch.setattr(Daemon, "_keep_for_cwd", lambda self, tx, job_id, attempt_id=None: None)
    resume_admitted(observed)
