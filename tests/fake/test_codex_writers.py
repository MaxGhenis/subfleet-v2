"""Codex thread identity does not manufacture a second process instance."""
from __future__ import annotations

import pytest

from subfleet.adapters.base import AdapterError
from tests.fake.test_state_contract import state_daemon
from tests.fake.test_writable_fanout import fleet, make_repo, writable


def test_codex_thread_can_submit_overlapping_distinct_writers_without_pid(fleet):
    """A2's new caller_session must not turn Codex fanout into unidentified-writer."""
    daemon, harness, _ = fleet
    first = writable(daemon, harness, make_repo(harness.root / "a"), pid=None, caller_family="codex")
    second = writable(daemon, harness, make_repo(harness.root / "b"), pid=None, caller_family="codex")
    assert first != second
    assert [row["caller_family"] for row in daemon.store.list_jobs()] == ["codex", "codex"]
    assert daemon.store.get_job(first)["caller_session"] == daemon.store.get_job(second)["caller_session"]


def test_claude_thread_still_refuses_unidentified_second_writer(fleet):
    """The Codex exception leaves the Claude process-instance check in force."""
    daemon, harness, _ = fleet
    writable(daemon, harness, make_repo(harness.root / "a"), pid=None, caller_family="claude")
    with pytest.raises(AdapterError) as error:
        writable(daemon, harness, make_repo(harness.root / "b"), pid=None, caller_family="claude")
    assert error.value.reason == "unidentified-writer"


def test_codex_writers_still_hold_their_worktrees(fleet):
    """Unknown Codex process identity cannot relax the exclusive worktree hold."""
    daemon, harness, _ = fleet
    repo = make_repo(harness.root / "a")
    writable(daemon, harness, repo, pid=None, caller_family="codex")
    with pytest.raises(AdapterError) as error:
        writable(daemon, harness, repo, pid=None, caller_family="codex")
    assert error.value.reason == "worktree-writer"


def test_codex_writers_still_observe_explicit_session_cap(fleet):
    """Only the instance comparison is skipped; an operator's session cap remains."""
    daemon, harness, _ = fleet
    daemon.policy["caps"]["max_writable_per_session"] = 1
    writable(daemon, harness, make_repo(harness.root / "a"), pid=None, caller_family="codex")
    with pytest.raises(AdapterError) as error:
        writable(daemon, harness, make_repo(harness.root / "b"), pid=None, caller_family="codex")
    assert error.value.reason == "writer-cap"
