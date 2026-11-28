"""A Codex thread may fan out writable jobs to distinct worktrees (C-6.5)."""

import pytest

from subfleet.adapters.base import AdapterError
from subfleet import cli
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_writable_fanout import fleet, make_repo, writable  # noqa: F401


def test_codex_thread_fans_out_without_a_claude_process_id(fleet, monkeypatch):
    daemon, harness, _ = fleet
    monkeypatch.setenv('CODEX_THREAD_ID', 'synthetic-codex-thread')
    for name in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PID'):
        monkeypatch.delenv(name, raising=False)
    jobs = [writable(daemon, harness, make_repo(harness.root / f'codex-{n}'),
                     pid=cli.caller_pid(), session=cli.session_id(),
                     caller_family=cli.caller_family()) for n in range(2)]
    # The exemption must not relax the worktree, revive or configured cap.
    with pytest.raises(AdapterError) as target:
        writable(daemon, harness, harness.root / 'codex-0', pid=None,
                 session=cli.session_id(), caller_family='codex')
    assert target.value.reason == 'worktree-writer'
    daemon.policy['caps']['max_writable_per_session'] = 2
    with pytest.raises(AdapterError) as capped:
        writable(daemon, harness, make_repo(harness.root / 'codex-capped'), pid=None,
                 session=cli.session_id(), caller_family='codex')
    assert capped.value.reason == 'writer-cap'
    daemon.policy['caps']['max_writable_per_session'] = None
    with pytest.raises(AdapterError) as revived:
        writable(daemon, harness, make_repo(harness.root / 'codex-revive'), pid=None,
                 session=cli.session_id(), caller_family='codex', kind='revive')
    assert revived.value.reason == 'revive-writer'
    daemon._admit()
    assert [daemon.store.get_job(job)['state'] for job in jobs] == ['running', 'running']
    assert all(daemon.store.get_job(job)['caller_session'] == 'synthetic-codex-thread'
               for job in jobs)
