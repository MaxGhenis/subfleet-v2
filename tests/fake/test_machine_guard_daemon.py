"""C-6.13: real admission priming, legacy gating and bounded calibration history."""
import copy
import json
import pytest
from subfleet import machine
from subfleet.daemon import Daemon
from subfleet.policy import MACHINE_GUARD_PROPOSAL
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_admission_priority import submit,reservation_order


def test_first_detached_pass_sees_primed_eighteen_busy_cpus(state_daemon,monkeypatch):
    old,harness=state_daemon
    policy=copy.deepcopy(old.policy); policy['admission']['machine_guard']=copy.deepcopy(MACHINE_GUARD_PROPOSAL)
    (old.root/'policy.json').write_text(json.dumps(policy)); root=old.root; old.close()
    now=[0.0]; calls=[]
    def ticks():
        calls.append(1)
        return tuple((int(now[0]*100),0,0,0) for _ in range(18))
    real=machine.Sampler
    monkeypatch.setattr(machine,'Sampler',lambda root=None,history=():real(root,clock=lambda:now[0],history=history))
    monkeypatch.setattr(machine,'_cpu_ticks',ticks)
    monkeypatch.setattr(machine,'_disk_counters',lambda:None)
    monkeypatch.setattr(machine.time,'sleep',lambda dt:now.__setitem__(0,now[0]+dt))
    daemon=Daemon(root)
    try:
        jobs=[submit(daemon,harness,f'primed-{i}',caller_session='gone') for i in range(3)]
        daemon._admit()
        assert len(calls)==2
        assert reservation_order(daemon)==[]
        for job in jobs: assert daemon._holds[job]['cpu_idle']==0
    finally: daemon.close()


@pytest.mark.parametrize('guard',[None,{'background':{'load_per_cpu':6,'memory_pressure':'warn'}}])
def test_legacy_policy_does_no_new_native_reads(state_daemon,monkeypatch,guard):
    daemon,harness=state_daemon; daemon.policy['admission']['machine_guard']=guard
    def forbidden(): pytest.fail('new native read under legacy policy')
    monkeypatch.setattr(machine,'_cpu_ticks',forbidden); monkeypatch.setattr(machine,'_disk_counters',forbidden)
    submit(daemon,harness,'legacy',caller_session='gone')
    daemon._admit()


def test_status_and_store_expose_aged_samples_and_survive_restart(state_daemon,monkeypatch):
    daemon,_=state_daemon
    daemon.policy['admission']['machine_guard']=copy.deepcopy(MACHINE_GUARD_PROPOSAL)
    now=[0.0]; real=machine.Sampler
    monkeypatch.setattr(machine,'Sampler',lambda root=None,history=():real(root,clock=lambda:now[0],history=history))
    monkeypatch.setattr(machine,'_cpu_ticks',lambda:((int(now[0]*50),0,int(now[0]*50),0),))
    monkeypatch.setattr(machine,'_disk_counters',lambda:{1:(int(now[0]*20e9),0,int(now[0]*10000),0)})
    monkeypatch.setattr(machine.time,'sleep',lambda dt:now.__setitem__(0,now[0]+dt))
    daemon._start_machine_sampler()
    daemon._machine_sampler.device_id=1
    for _ in range(3):
        now[0]+=1
        daemon._machine_reading()
    rows=daemon.store.query("SELECT data_json FROM events WHERE kind='machine.sample'")
    assert len(rows)==1
    view=daemon.dispatch('daemon.status',{})['machine_guard']
    for key in ('cpu_idle','disk_busy','disk_in_flight','disk_baseline_ms','read_cost_s'):
        assert view[key]['value'] is not None and view[key]['age_s']>=0
    assert view['disk_busy']['unit']=='ms/op'
    saved=json.loads(rows[0]['data_json'])
    restored=real(daemon.root,history=[saved])
    assert restored.baseline_ms==pytest.approx(2)


def test_sample_retention_is_bounded_and_restarts_cannot_exceed_minute(state_daemon,monkeypatch):
    daemon,_=state_daemon
    daemon.policy['admission']['machine_guard']=copy.deepcopy(MACHINE_GUARD_PROPOSAL)
    with daemon.store.transaction('test.seed') as tx:
        for i in range(1442):
            tx.execute("INSERT INTO events(ts,kind,data_json) VALUES(?, 'machine.sample', ?)",('2000-01-01T00:00:00Z',json.dumps({'observed_at':i,'disk_service_ms_raw':2,'disk_device_id':1})))
    monkeypatch.setattr(machine.time,'sleep',lambda _:None)
    daemon._start_machine_sampler()
    daemon._machine_reading()
    assert len(daemon.store.query("SELECT 1 FROM events WHERE kind='machine.sample'"))<=1440
    before=len(daemon.store.query("SELECT 1 FROM events WHERE kind='machine.sample'"))
    daemon._start_machine_sampler(); daemon._machine_reading()
    assert len(daemon.store.query("SELECT 1 FROM events WHERE kind='machine.sample'"))==before
