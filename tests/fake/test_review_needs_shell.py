"""D-SH1: shell requirements govern admission, pins and launch evidence."""

import json
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import cli, scheduler
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from tests.fake.test_state_contract import receipt_fixture, state_daemon  # noqa: F401
from tests.fake.test_workspace_contract import repository


@pytest.fixture
def shell_state(state_daemon, monkeypatch):
    service, harness = state_daemon
    from subfleet.adapters import registry
    from tests.fake_adapter import FakeAdapter
    monkeypatch.setitem(registry._factories, "claude", FakeAdapter)
    service.policy['models'].update(sol61={'provider': 'codex', 'id': 'gpt-6.1-sol'},
                                    luna={'provider': 'codex', 'id': 'gpt-6-luna'})
    service.policy['chains']['review'][-1] = ['sol61', 'opus']
    service.policy['requires'] = {'review': ['shell']}
    service.policy['caps'].update(max_in_flight_per_lane=1, max_in_flight_unmeasured=1)
    service.store.put_lane(Lane('claude-10', 'claude', 'claude:fixture',
        Credential('claude', '/fake/claude-10', 'home'), '/fake/claude-10', LaneOwner.V2, False))
    for identity in ('codex-1', 'claude-10'):
        service.store.add_reading(Reading(identity, 'account', 'seven_day', .1, after(3600),
                                          ReadingLabel.PROVIDER, 'fixture', utcnow()))
    return service, harness


def review(service, harness, **changes):
    return service.dispatch('submit', harness.submit_args(pinned_model=None, task='review',
                            tier='hard', **changes))['job_id']


def test_hard_review_waits_for_codex_then_runs_with_shell(shell_state):
    service, harness = shell_state
    busy = service.dispatch('submit', harness.submit_args())['job_id']
    service._admit()
    job = review(service, harness)
    service._admit()
    assert not service.store.list_attempts(job)
    waiting = service.store.get_job(job)
    assert (waiting['state'], waiting['wait_reason']) == ('waiting', 'capacity')
    assert 'needs a shell: opus runs read-only jobs without one' in service.dispatch('why', {'job_id': job})['text']
    attempt = service.store.list_attempts(busy)[0]
    service.store.update_attempt(attempt['attempt_id'], state='succeeded')
    with service.store.transaction('fixture.freed') as tx:
        tx.execute('DELETE FROM leases WHERE holder=?', (attempt['attempt_id'],))
    service._admit()
    placed, = service.store.list_attempts(job)
    assert (placed['lane_id'], placed['model_requested'], placed['has_shell']) == ('codex-1', 'gpt-6.1-sol', True)


@pytest.mark.parametrize('sandbox,override,expected', [
    ('read-only', False, False), ('workspace-write', None, True),
])
def test_opus_attempt_and_first_notice_line_report_shell(shell_state, sandbox, override, expected):
    service, harness = shell_state
    if sandbox == 'workspace-write':
        repository(service, harness)
    service.dispatch('submit', harness.submit_args())
    service._admit()
    changes = {'sandbox': sandbox, 'out_path': str(harness.root / 'result.md')}
    if override is not None:
        changes['needs_shell'] = override
    job = review(service, harness, **changes)
    service._admit()
    attempt, = service.store.list_attempts(job)
    assert attempt['lane_id'] == 'claude-10'
    assert attempt['has_shell'] is expected
    adir = service.root / 'jobs' / job / 'a1'
    adir.mkdir()
    service._finalize(receipt_fixture(service, attempt, adir))
    service._export(job)
    shown = service.dispatch('show', {'job_id': job})
    assert shown['attempts'][0]['has_shell'] is expected
    text = f"ran {sandbox} on claude-10 (opus) with {'a shell' if expected else 'no shell'}"
    notice, = [n for n in service.store.list_notices() if n['job_id'] == job]
    assert text in notice['text'].splitlines()[0]
    assert text in cli._wait_summary(shown)
    assert (harness.root / 'result.md').read_text() == 'fixture result\n'
    assert (adir / 'deliverable.md').read_text() == 'fixture result\n'


@pytest.mark.parametrize('model', ['sol61', 'luna'])
def test_new_policy_models_route_to_codex(shell_state, model):
    service, harness = shell_state
    job = service.dispatch('submit', harness.submit_args(pinned_model=model))['job_id']
    service._admit()
    attempt, = service.store.list_attempts(job)
    assert attempt['model_requested'] == service.policy['models'][model]['id']


@pytest.mark.parametrize('changes', [
    {'pinned_model': 'opus', 'needs_shell': True},
    {'pinned_model': 'opus', 'task': 'review'},
    {'pinned_model': None, 'pinned_lane': 'claude-10', 'task': 'review', 'tier': 'standard'},
])
def test_unsatisfiable_read_only_pin_refused_before_store(shell_state, changes):
    service, harness = shell_state
    with pytest.raises(AdapterError) as refused:
        service.dispatch('submit', harness.submit_args(**changes))
    assert refused.value.code == 7
    assert 'needs a shell' in str(refused.value)
    assert '-s workspace-write' in refused.value.fix and 'Codex' in refused.value.fix
    assert not service.store.list_jobs()


def test_unknown_pin_is_daemon_refusal_listing_policy_models(shell_state):
    service, harness = shell_state
    with pytest.raises(AdapterError) as refused:
        service.dispatch('submit', harness.submit_args(pinned_model='absent'))
    assert refused.value.code == 7
    assert all(name in str(refused.value) for name in service.policy['models'])
    assert not service.store.list_jobs()


def test_shell_override_is_stored_and_part_of_idempotency(shell_state):
    service, harness = shell_state
    args = harness.submit_args(pinned_model=None, task='review', tier='hard', needs_shell=False)
    job = service.dispatch('submit', args)['job_id']
    assert service.store.get_job(job)['needs_shell'] is False
    assert service.dispatch('submit', args)['job_id'] == job
    from subfleet.protocol import ProtocolError
    with pytest.raises(ProtocolError, match='different payload'):
        service.dispatch('submit', {**args, 'needs_shell': True})


@pytest.mark.parametrize('bad', ['true', 1, [], {}])
def test_shell_override_must_be_boolean(shell_state, bad):
    service, harness = shell_state
    from subfleet.protocol import ProtocolError
    with pytest.raises(ProtocolError, match='needs_shell'):
        service.dispatch('submit', harness.submit_args(needs_shell=bad))


# Freeze the base candidate builder itself, not an oracle built from the new filter.
_BASE = dict(vars(scheduler))
exec(compile((Path(__file__).parents[1] / 'fixtures/review_shell_routing_before.py').read_text(),
             'review_shell_routing_before.py', 'exec'), _BASE)


@st.composite
def routing_cases(draw):
    models = {'sol61': {'provider': 'codex', 'id': 'gpt-6.1-sol'},
              'opus': {'provider': 'claude', 'id': 'claude-opus-5-5'},
              'luna': {'provider': 'codex', 'id': 'gpt-6-luna'}}
    tiers = ['trivial', 'easy', 'standard', 'hard']
    entry = st.one_of(st.sampled_from(list(models)),
                      st.lists(st.sampled_from(list(models)), min_size=1, max_size=3, unique=True))
    chain = draw(st.lists(entry, min_size=4, max_size=4))
    lanes = []
    for number, provider in enumerate(draw(st.lists(st.sampled_from(['claude', 'codex']), max_size=6))):
        lanes.append({'lane_id': f'{provider}-{number}', 'provider': provider, 'owner': 'v2',
                      'enabled': draw(st.booleans()), 'account_key': f'{provider}:{number}'})
    sandbox = draw(st.sampled_from(['read-only', 'workspace-write']))
    policy = {'models': models, 'retired': {}, 'tiers': tiers, 'chains': {'review': chain},
              'caps': {'max_in_flight_per_lane': 1, 'max_in_flight_unmeasured': 1},
              'requires': {'review': draw(st.sampled_from([[], ['shell']]))}}
    job = {'task': 'review', 'tier': draw(st.sampled_from(tiers)), 'sandbox': sandbox,
           'needs_shell': draw(st.sampled_from([None, True, False])),
           'exclusions': draw(st.lists(st.sampled_from([lane['lane_id'] for lane in lanes]), max_size=3)) if lanes else []}
    view = {'now': '2026-10-14T12:00:00Z', 'lanes': lanes,
            'in_flight': {lane['lane_id']: draw(st.integers(0, 2)) for lane in lanes}}
    return policy, view, job


BOUNDED = settings(max_examples=80, derandomize=True, database=None, deadline=None)


@BOUNDED
@given(routing_cases())
def test_needs_shell_only_places_on_capable_launch_or_waits(case):
    policy, view, job = case
    decision = scheduler.evaluate(policy, view, job)
    needs = job['needs_shell'] if job['needs_shell'] is not None else 'shell' in policy['requires']['review']
    if decision.chosen_lane and needs:
        provider = policy['models'][decision.chosen_model]['provider']
        assert provider == 'codex' or job['sandbox'] == 'workspace-write'


@BOUNDED
@given(routing_cases())
def test_no_requirement_routes_exactly_as_base(case):
    policy, view, job = case
    job['needs_shell'] = False
    assert scheduler.evaluate(policy, view, job) == _BASE['evaluate'](policy, view, job)


def test_requirement_can_skip_opus_for_later_codex_model():
    policy = {'models': {'opus': {'provider': 'claude', 'id': 'opus'},
                         'luna': {'provider': 'codex', 'id': 'luna'}},
              'retired': {}, 'tiers': ['hard'], 'chains': {'review': [['opus', 'luna']]}}
    view = {'lanes': [{'lane_id': p, 'provider': p, 'owner': 'v2'} for p in ['claude', 'codex']]}
    result = scheduler.evaluate(policy, view, {'task': 'review', 'tier': 'hard',
                                'sandbox': 'read-only', 'needs_shell': True})
    assert result.chosen_model == 'luna'


def test_resolved_shell_requirement_survives_policy_change(shell_state):
    service, harness = shell_state
    job = review(service, harness)
    assert service.store.get_job(job)['needs_shell'] is True
    service.policy['requires'] = {}
    service.store.update_lane('codex-1', enabled=False)
    service._admit()
    assert not service.store.list_attempts(job)
    assert service.store.get_job(job)['wait_reason'] == 'capacity'


def test_shell_requirement_demand_does_not_compete_for_opus():
    policy = {'models': {'opus': {'provider': 'claude', 'id': 'opus'},
                         'luna': {'provider': 'codex', 'id': 'luna'}},
              'retired': {}, 'tiers': ['hard'], 'chains': {'review': [['opus', 'luna']]}}
    assert scheduler.demand_models(policy, {'task': 'review', 'tier': 'hard',
        'sandbox': 'read-only', 'needs_shell': True}) == frozenset({'luna'})



def test_resume_keeps_explicit_shell_opt_out(shell_state):
    service, harness = shell_state
    source = service.dispatch('submit', harness.submit_args(pinned_model='opus', task='review', needs_shell=False))['job_id']
    service._admit()
    attempt, = service.store.list_attempts(source)
    service.store.update_attempt(attempt['attempt_id'], native_session_id='fixture-native-session')
    adir = service.root / 'jobs' / source / 'a1'
    adir.mkdir()
    service._finalize(receipt_fixture(service, attempt, adir))
    resumed = service.dispatch('submit', harness.submit_args(kind='resume', parent_job_id=source))['job_id']
    assert service.store.get_job(resumed)['needs_shell'] is False
    service._admit()
    continued, = service.store.list_attempts(resumed)
    assert continued['lane_id'] == 'claude-10' and continued['has_shell'] is False
