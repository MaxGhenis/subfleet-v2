"""D-WALL-RETRY1: outside-fault retries retain each attempt's wall cap."""

from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters.registry import register
from subfleet.contracts import Credential, Outcome, OutcomeClass
from subfleet.daemon import LIVE_TICK
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401
from tests.fake.test_wall_running_time import NOW, cancel_events, stamp
from tests.fake_adapter import FakeAdapter


class Limited(FakeAdapter):
    def classify(self, attempt_dir, launch, exit_info):
        return Outcome(OutcomeClass.LIMITED, 'fixture provider limit')


class Transient(FakeAdapter):
    def classify(self, attempt_dir, launch, exit_info):
        return Outcome(OutcomeClass.TRANSIENT, 'fixture transport disconnected')


class LaneFault(FakeAdapter):
    def classify(self, attempt_dir, launch, exit_info):
        return Outcome(OutcomeClass.AUTH_DEAD, 'fixture lane access revoked',
                       evidence={'model_answered': False})


@pytest.fixture
def retry_wall(state_daemon, monkeypatch):
    service, harness = state_daemon
    clock = [NOW]

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(daemon_module, 'datetime', FrozenDatetime)
    monkeypatch.setattr(service, '_read_answer', lambda *args: None)
    monkeypatch.setattr(service, '_inspect_running', lambda *args: True)
    monkeypatch.setattr(service.conversations, 'stop', lambda *args: False)
    first_lane = service.store.get_lane('codex-1')
    home = str(harness.root / 'home-codex-2')
    service.store.put_lane(replace(first_lane, lane_id='codex-2', account_key='codex:wall-retry',
                                   credential=Credential('codex', home, 'home'), home=home))

    def advance(seconds):
        clock[0] += timedelta(seconds=seconds)

    return service, harness, advance


def finish(service, attempt, adapter, *, rc=1, killed_by=None):
    """Drive real finalization with an exited fake provider's class."""
    adir = service.root / 'jobs' / attempt['job_id'] / f"a{attempt['seq']}"
    adir.mkdir(exist_ok=True)
    register('codex', adapter)
    try:
        finalizing = receipt_fixture(service, attempt, adir, rc=rc)
        if killed_by:
            receipt = json.loads((adir / 'exit.json').read_text())
            receipt.update(signal=15, killed_by=killed_by)
            (adir / 'exit.json').write_text(json.dumps(receipt))
        service._finalize(finalizing)
    finally:
        register('codex', FakeAdapter)


def next_running(service, job_id):
    """Use admission for the retry; stand in for its successful provider launch."""
    service._admit()
    attempt = service.store.list_attempts(job_id)[-1]
    service._pending_launches.discard(attempt['attempt_id'])
    adir = service.root / 'jobs' / job_id / f"a{attempt['seq']}"
    adir.mkdir(exist_ok=True)
    service.store.update_attempt(attempt['attempt_id'], state='running',
                                 started_at=daemon_module.utcnow())
    service.store.update_job(job_id, state='running')
    return service.store.get_attempt(attempt['attempt_id'])


def test_limited_retry_has_its_own_full_wall_cap(retry_wall, monkeypatch):
    service, harness, advance = retry_wall
    jid, first, _ = reserve(service, harness, max_wall_s=10, max_attempts=2)
    service.store.update_attempt(first['attempt_id'], state='running', started_at=stamp(-6))
    finish(service, first, Limited)
    assert service.store.get_attempt(first['attempt_id'])['outcome_class'] == 'limited'
    assert service.store.get_job(jid)['state'] == 'waiting'
    second = next_running(service, jid)
    assert second['seq'] == 2
    stopped = []
    monkeypatch.setattr(service, '_kill_attempt', lambda a: stopped.append(a['attempt_id']))

    advance(5)  # The old sum is 11 seconds; this attempt has used only five.
    service._process_attempt(second['attempt_id'])
    assert cancel_events(service, jid) == []
    assert stopped == []
    assert service._running_s(jid) == 5
    advance(5)
    row = next(row for row in service.store.query(LIVE_TICK)
               if row['attempt_id'] == second['attempt_id'])
    assert service._has_work(row)
    service._process_attempt(second['attempt_id'])
    assert stopped == [second['attempt_id']]
    assert service.store.get_attempt(second['attempt_id'])['killed_by'] == 'max_wall_s'
    assert cancel_events(service, jid) == [
        {'by': 'daemon', 'reason': 'max_wall_s', 'running_s': 10.0}]


def test_lost_retry_still_charges_the_prior_attempt(retry_wall, monkeypatch):
    service, harness, advance = retry_wall
    jid, first, _ = reserve(service, harness, max_wall_s=10, max_attempts=2)
    service.store.update_attempt(first['attempt_id'], state='running', started_at=stamp(-6),
                                 evidence_json=json.dumps({'last_observed_alive_at': stamp(0)}))
    service._lost(service.store.get_attempt(first['attempt_id']))
    assert service.store.get_attempt(first['attempt_id'])['state'] == 'lost'
    assert service.store.get_job(jid)['state'] == 'waiting'
    assert service._running_s(jid) == 6
    second = next_running(service, jid)
    assert second['seq'] == 2
    stopped = []
    monkeypatch.setattr(service, '_kill_attempt', lambda a: stopped.append(a['attempt_id']))

    advance(3)
    service._process_attempt(second['attempt_id'])
    assert cancel_events(service, jid) == []
    advance(1)
    service._process_attempt(second['attempt_id'])
    assert stopped == [second['attempt_id']]
    assert cancel_events(service, jid) == [
        {'by': 'daemon', 'reason': 'max_wall_s', 'running_s': 10.0}]


def test_transient_overrun_counts_its_own_time_after_classification(retry_wall):
    service, harness, advance = retry_wall
    jid, first, _ = reserve(service, harness, max_wall_s=10, max_attempts=2)
    service.store.update_attempt(first['attempt_id'], state='running', started_at=stamp(-6))
    finish(service, first, Limited)
    second = next_running(service, jid)
    advance(11)  # The daemon missed this provider's cap before its own exit.
    finish(service, second, Transient)

    attempt = service.store.get_attempt(second['attempt_id'])
    assert (attempt['state'], attempt['outcome_class'], attempt['killed_by']) == (
        'failed', 'transient', None)
    assert json.loads(attempt['evidence_json'])['wall_overrun'] == {
        'running_s': 11.0, 'max_wall_s': 10.0, 'overrun_s': 1.0,
        'provider_finished_at': stamp(11),
        'reason': 'provider exited before the daemon enforced the wall'}
    assert service._running_s(jid) == 0
    assert cancel_events(service, jid) == []


def test_lane_fault_extra_attempt_has_its_own_wall_cap(retry_wall, monkeypatch):
    service, harness, advance = retry_wall
    jid, first, _ = reserve(service, harness, max_wall_s=10, max_attempts=1)
    service.store.update_attempt(first['attempt_id'], state='running', started_at=stamp(-6))
    finish(service, first, LaneFault)
    fault = service.store.get_attempt(first['attempt_id'])
    assert json.loads(fault['evidence_json'])['lane_fault']['class'] == 'auth-dead'
    assert service.store.get_job(jid)['state'] == 'waiting'
    second = next_running(service, jid)
    assert (second['seq'], second['lane_id']) == (2, 'codex-2')
    stopped = []
    monkeypatch.setattr(service, '_kill_attempt', lambda a: stopped.append(a['attempt_id']))

    advance(5)
    service._process_attempt(second['attempt_id'])
    assert stopped == []
    assert cancel_events(service, jid) == []
    advance(5)
    service._process_attempt(second['attempt_id'])
    assert stopped == [second['attempt_id']]
    assert cancel_events(service, jid) == [
        {'by': 'daemon', 'reason': 'max_wall_s', 'running_s': 10.0}]
    finish(service, service.store.get_attempt(second['attempt_id']), Transient,
           killed_by='max_wall_s')
    assert service.store.get_attempt(second['attempt_id'])['state'] == 'interrupted'
    assert service.store.get_job(jid)['state'] == 'cancelled'
    service._admit()
    attempts = service.store.list_attempts(jid)
    assert len(attempts) == 2  # max_attempts=1 plus one lane-fault attempt.
    total_provider_s = sum((datetime.fromisoformat(a['finished_at']) -
                            datetime.fromisoformat(a['started_at'])).total_seconds()
                           for a in attempts)
    assert total_provider_s == 16
    assert total_provider_s <= len(attempts) * 10
