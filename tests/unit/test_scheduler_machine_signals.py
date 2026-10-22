"""C-6.13 invariants over bounded timestamped reading sequences."""
import copy
import pytest
from hypothesis import HealthCheck,given,settings,strategies as st
from subfleet import scheduler
from subfleet.policy import MACHINE_GUARD_PROPOSAL
from tests.machine_guard_reference import LEGACY_GUARD,machine_hold as previous_hold
CI=settings(max_examples=50,deadline=None,derandomize=True,database=None,suppress_health_check=[HealthCheck.too_slow])
F=st.floats(0,1,allow_nan=False,allow_infinity=False)
BAD=st.sampled_from([None,True,False,-1,float('nan'),float('inf'),'busy',10**400])
SEQ=st.lists(st.tuples(st.floats(.01,15,allow_nan=False,allow_infinity=False),st.one_of(F,st.none()),st.one_of(st.floats(0,50,allow_nan=False,allow_infinity=False),st.none())),min_size=1,max_size=35)

def policy(**limits): return {'admission':{'machine_guard':{'background':limits}}}
def held(state,chosen,at=0,**reading):
    return scheduler.machine_hold(chosen,state.advance(chosen,dict(reading,sampled_at=at)),'background')

@CI
@given(SEQ)
def test_stateful_class_ordering_and_never_held_exemptions(sequence):
    chosen={'admission':{'machine_guard':{'background':{'cpu_idle_min':.15,'disk_busy_max':4},'session':{'cpu_idle_min':.05,'disk_busy_max':8}}}}
    state=scheduler.MachineGuard(); now=0
    for dt,idle,disk in sequence:
        now+=dt; row=state.advance(chosen,{'cpu_idle':idle,'disk_busy':disk,'sampled_at':now})
        if scheduler.machine_hold(chosen,row,'session'): assert scheduler.machine_hold(chosen,row,'background')
        for klass in ('attended','priority','unknown'): assert scheduler.machine_hold(chosen,row,klass) is None

@CI
@given(SEQ)
def test_monotone_cpu_and_disk_histories(sequence):
    chosen=policy(cpu_idle_min=.15,disk_busy_max=4)
    lo,hi=scheduler.MachineGuard(),scheduler.MachineGuard(); now=0
    for dt,idle,disk in sequence:
        now+=dt
        low=held(lo,chosen,now,cpu_idle=None if idle is None else idle/2,disk_busy=None if disk is None else disk+1)
        high=held(hi,chosen,now,cpu_idle=idle,disk_busy=disk)
        if high: assert low

@CI
@given(BAD,st.sampled_from([('cpu_idle','cpu_idle_min',.15,0,.9),('disk_busy','disk_busy_max',4,10,1)]))
def test_unreadable_never_holds_or_releases_latch(value,signal):
    name,key,threshold,busy,quiet=signal; chosen=policy(**{key:threshold}); state=scheduler.MachineGuard()
    assert held(state,chosen,0,**{name:busy})
    assert held(state,chosen,1,**{name:value}) is None
    assert scheduler.machine_hold(chosen,{name:value},'background') is None
    assert held(state,chosen,2,**{name:quiet})  # latch survived; begins timed recovery

@CI
@given(st.lists(st.floats(.001,.2,allow_nan=False,allow_infinity=False),min_size=2,max_size=30))
def test_release_requires_ten_seconds_not_two_passes(intervals):
    state=scheduler.MachineGuard(); chosen=policy(cpu_idle_min=.15); now=0
    assert held(state,chosen,now,cpu_idle=0)
    for dt in intervals:
        now+=dt
        assert held(state,chosen,now,cpu_idle=.9)
    assert held(state,chosen,now+10,cpu_idle=.9) is None

@CI
@given(st.floats(.01,.9,allow_nan=False,allow_infinity=False))
def test_cpu_release_needs_margin_and_continuous_time(threshold):
    state=scheduler.MachineGuard(); chosen=policy(cpu_idle_min=threshold)
    assert held(state,chosen,0,cpu_idle=0)
    assert held(state,chosen,1,cpu_idle=threshold+.04)
    assert held(state,chosen,10,cpu_idle=threshold+.015) # interrupts recovery
    assert held(state,chosen,11,cpu_idle=threshold+.04)
    assert held(state,chosen,20.9,cpu_idle=threshold+.04)
    assert held(state,chosen,21,cpu_idle=threshold+.04) is None

@CI
@given(st.floats(1,100,allow_nan=False,allow_infinity=False))
def test_disk_release_needs_ten_percent_margin(threshold):
    state=scheduler.MachineGuard(); chosen=policy(disk_busy_max=threshold)
    assert held(state,chosen,0,disk_busy=threshold*2)
    assert held(state,chosen,1,disk_busy=threshold*.95)
    assert held(state,chosen,20,disk_busy=threshold*.95)
    assert held(state,chosen,21,disk_busy=threshold*.8)
    assert held(state,chosen,31,disk_busy=threshold*.8) is None

@CI
@given(SEQ)
def test_release_is_never_earlier_than_hold_plus_ten_seconds(sequence):
    state=scheduler.MachineGuard(); chosen=policy(cpu_idle_min=.15); now=0; last_busy=None
    for dt,idle,_ in sequence:
        now+=dt; hold=held(state,chosen,now,cpu_idle=idle)
        if idle is not None and idle<.15: last_busy=now
        if last_busy is not None and now<last_busy+10 and idle is not None: assert hold

LEGACY=st.one_of(st.none(),st.fixed_dictionaries({'load1':st.one_of(st.none(),st.floats()),'load5':st.one_of(st.none(),st.floats()),'cpus':st.one_of(st.none(),st.integers(-1,256)),'memory_pressure':st.one_of(st.none(),st.integers(-1,10))}))
@CI
@given(st.lists(LEGACY,min_size=1,max_size=12),st.sampled_from([None,{},LEGACY_GUARD,{'background':{'load_per_cpu':.01}},{'session':{'memory_pressure':'warn'}}]))
def test_legacy_only_guard_matches_473fe2941_on_every_generated_reading(readings,guard):
    chosen={'admission':{'machine_guard':copy.deepcopy(guard)}}; state=scheduler.MachineGuard()
    for reading in readings:
        row=state.advance(chosen,reading)
        for klass in ('attended','priority','session','background','unknown'):
            assert scheduler.machine_hold(chosen,row,klass)==previous_hold(chosen,reading,klass)


def test_lost_read_during_recovery_restarts_only_release_timer():
    state=scheduler.MachineGuard(); chosen=policy(cpu_idle_min=.15)
    assert held(state,chosen,0,cpu_idle=0)
    assert held(state,chosen,1,cpu_idle=.9)
    assert held(state,chosen,10,cpu_idle=None) is None
    assert held(state,chosen,11,cpu_idle=.9)
    assert held(state,chosen,20,cpu_idle=.9)
    assert held(state,chosen,21,cpu_idle=.9) is None


def test_multiple_jobs_and_reused_snapshot_cannot_advance_recovery():
    chosen=policy(cpu_idle_min=.15); state=scheduler.MachineGuard()
    assert held(state,chosen,0,cpu_idle=0)
    row=state.advance(chosen,{'cpu_idle':.9,'sampled_at':1})
    for _ in range(50):
        assert scheduler.machine_hold(chosen,row,'background')
        assert held(state,chosen,1,cpu_idle=.9)
    assert held(state,chosen,11,cpu_idle=.9) is None
    assert held(state,policy(cpu_idle_min=.1),12,cpu_idle=.2) is None


def test_incidents_and_uncalibrated_disk_proposal():
    chosen={'admission':{'machine_guard':MACHINE_GUARD_PROPOSAL}}
    assert all('disk_busy_max' not in limits for limits in MACHINE_GUARD_PROPOSAL.values())
    for klass in ('background','session'):
        assert scheduler.machine_hold(chosen,{'load1':398,'load5':316,'cpus':18,'cpu_idle':.32,'disk_busy':20},klass) is None
    assert scheduler.machine_hold(chosen,{'cpu_idle':0},'session')['cpu_idle']==0


def test_new_hold_text_names_units_and_timed_release():
    from subfleet.render import _machine
    state=scheduler.MachineGuard(); chosen=policy(cpu_idle_min=.15,disk_busy_max=4)
    h=held(state,chosen,0,cpu_idle=.01,disk_busy=5)
    assert 'CPU idle 1.0%' in _machine(h) and 'ms/op' in _machine(h)
    h=held(state,chosen,1,cpu_idle=.5,disk_busy=1)
    assert '10' in _machine(h) and 'seconds' in _machine(h)
