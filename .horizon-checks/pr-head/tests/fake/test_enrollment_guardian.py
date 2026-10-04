"""Re-authentication uses the real guardian fence, with a Python fake provider."""
import os
from pathlib import Path
import sys

from subfleet.daemon import Daemon
from tests.fake.conftest import Harness


def test_enrollment_commits_guardian_identity_before_fake_provider_runs(tmp_path, process_inspection_available):
    root = tmp_path / 'state'
    root.mkdir()
    Harness(root)  # Creates only an isolated fake roster and policy.
    daemon = Daemon(root)
    holder = 'probe:timer:enroll:owned-test'
    lane_id = 'codex-1'
    daemon.store.acquire_lease(f'lane:{lane_id}:slot:0', holder)
    daemon.timers.active_holders.add(holder)
    code = '''import json,os,sqlite3
with sqlite3.connect(os.environ['SUBFLEET_ROOT'] + '/state.sqlite3') as db:
    events = db.execute("SELECT data_json FROM events WHERE kind='probe.state' ORDER BY event_id DESC").fetchall()
    record = next(json.loads(row[0]) for row in events if json.loads(row[0]).get('holder') == os.environ['SUBFLEET_ATTEMPT'])
    assert record['state'] == 'starting'
    assert record['guardian_pid'] == os.getppid() and record['pgid'] == os.getpgrp()
print('authenticated-fake')
'''
    try:
        result = daemon._enrollment_turn(lane_id, holder, [sys.executable, '-c', code],
                                         cwd=str(tmp_path), env=dict(os.environ), timeout=10)
        assert result.returncode == 0 and result.stdout.strip() == 'authenticated-fake'
        record = daemon._probe_record(holder)
        assert record['state'] == 'contained'
        assert daemon._probe_census(record).verified_empty
        assert daemon.store.list_leases(holder), 'validation owns lease until new binding publication'
        assert daemon.store.list_jobs() == []
        daemon.timers.active_holders.discard(holder)
        daemon._recover_probes()
        assert not daemon.store.list_leases(holder)
    finally:
        daemon.timers.active_holders.discard(holder)
        if daemon._probe_record(holder):
            daemon._recover_probes()
        daemon.close()
