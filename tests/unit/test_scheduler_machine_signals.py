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


def test_recorded_ninety_second_ordinary_load_replay_holds_nothing():
    # AC, 2026-10-22: all 81 one-second snapshots from the bounded host recording.
    # cpu EWMA, service ms/op EWMA, uncapped in-flight requests, elapsed timestamp.
    recorded = [
        (0.319846, 1.662125, 22.274210, 0.000000),
        (0.350803, 1.868610, 27.392165, 1.034489),
        (0.357905, 2.028412, 33.792781, 2.133731),
        (0.372400, 1.988666, 26.059617, 3.224280),
        (0.350418, 2.015347, 26.032640, 4.409601),
        (0.350190, 1.896537, 21.066231, 5.604984),
        (0.346949, 2.076698, 32.607190, 6.718945),
        (0.347175, 2.224769, 29.805909, 7.808500),
        (0.350197, 2.163505, 22.156602, 8.999215),
        (0.343989, 2.207404, 25.795351, 10.026088),
        (0.334664, 2.217638, 20.340140, 11.217934),
        (0.344054, 2.222430, 22.195816, 12.218243),
        (0.346614, 2.230135, 19.778959, 13.401433),
        (0.346640, 2.263775, 21.000868, 14.590451),
        (0.339524, 2.223079, 20.098871, 15.601235),
        (0.339901, 2.216034, 20.333590, 16.694088),
        (0.331539, 2.173108, 18.997416, 17.823471),
        (0.331955, 2.170767, 20.177356, 18.915320),
        (0.335297, 2.158882, 18.835683, 19.995376),
        (0.327691, 2.145236, 18.615192, 21.124322),
        (0.329686, 2.084969, 6.950450, 22.287861),
        (0.328977, 2.058775, 17.947583, 23.387641),
        (0.328844, 2.059045, 19.210477, 24.404531),
        (0.325701, 2.034591, 16.755633, 25.586346),
        (0.325158, 2.005092, 17.506749, 26.735986),
        (0.327770, 2.227035, 76.977192, 27.901911),
        (0.329150, 2.211260, 23.705070, 29.003540),
        (0.330870, 2.190864, 22.629255, 30.103932),
        (0.332165, 2.170644, 24.041087, 31.110973),
        (0.334482, 2.139700, 28.638669, 32.213749),
        (0.336899, 2.110283, 24.002869, 33.405473),
        (0.338695, 2.082388, 24.279924, 34.440193),
        (0.339437, 2.038158, 20.627550, 35.692010),
        (0.340658, 2.010176, 25.060601, 36.832643),
        (0.337363, 2.014024, 36.708702, 37.906987),
        (0.331746, 2.022279, 43.077621, 39.122253),
        (0.335076, 2.081783, 58.499993, 40.199548),
        (0.335503, 2.104209, 44.340709, 41.208484),
        (0.331114, 2.090322, 25.274320, 42.400295),
        (0.332521, 2.076911, 24.387740, 43.427413),
        (0.333938, 2.049842, 16.800555, 44.429542),
        (0.334304, 2.024429, 19.438193, 45.507686),
        (0.336863, 1.995886, 18.062431, 46.708523),
        (0.337612, 1.961064, 19.378926, 47.935746),
        (0.338925, 1.942562, 17.104671, 48.988433),
        (0.338531, 1.924228, 17.892558, 50.110695),
        (0.335910, 1.909480, 24.833582, 51.228821),
        (0.334474, 1.917216, 27.775901, 52.439867),
        (0.334733, 1.906923, 17.477227, 53.524909),
        (0.336528, 1.909655, 21.101042, 54.714135),
        (0.336895, 1.926804, 22.590057, 55.737742),
        (0.332740, 1.946794, 23.238369, 56.806588),
        (0.331779, 1.977285, 25.592011, 58.089402),
        (0.327044, 1.981530, 27.906541, 59.109705),
        (0.327446, 1.995130, 24.538689, 60.134755),
        (0.327004, 1.981681, 18.340996, 61.298712),
        (0.326846, 1.964638, 17.652282, 62.397620),
        (0.326102, 1.952982, 10.421125, 63.496221),
        (0.327668, 1.939986, 9.230860, 64.678473),
        (0.328647, 1.917698, 9.803032, 65.775592),
        (0.329654, 1.905624, 8.799260, 66.910946),
        (0.329069, 1.928725, 25.086321, 68.000762),
        (0.330011, 1.945493, 26.953190, 69.202858),
        (0.330635, 1.959752, 27.827932, 70.290013),
        (0.329858, 1.979226, 25.377591, 71.306066),
        (0.326919, 2.011209, 35.232771, 72.399823),
        (0.326202, 2.037299, 36.108061, 73.440738),
        (0.326952, 2.068162, 33.545562, 74.631529),
        (0.330796, 2.111135, 33.350240, 75.732252),
        (0.336285, 2.132072, 27.776957, 76.877021),
        (0.339692, 2.155337, 30.731856, 78.085883),
        (0.337679, 2.171433, 37.909515, 79.093572),
        (0.336678, 2.167165, 29.304249, 80.216350),
        (0.337943, 2.214919, 39.688563, 81.394513),
        (0.336081, 2.236753, 29.331809, 82.402875),
        (0.335689, 2.224871, 26.251675, 83.421478),
        (0.337285, 2.208475, 20.214040, 84.442813),
        (0.338443, 2.199530, 19.527257, 85.601718),
        (0.338349, 2.227801, 47.310762, 86.823264),
        (0.339062, 2.210694, 20.347994, 87.930142),
        (0.339855, 2.195863, 21.245196, 89.123905),
    ]
    state=scheduler.MachineGuard(); chosen={"admission":{"machine_guard":MACHINE_GUARD_PROPOSAL}}
    for idle,disk,flight,at in recorded:
        assert flight > 1  # old term would be 1.000 at every read
        row=state.advance(chosen,{"cpu_idle":idle,"disk_busy":disk,"sampled_at":at})
        for klass in ("background","session"):
            assert scheduler.machine_hold(chosen,row,klass) is None
