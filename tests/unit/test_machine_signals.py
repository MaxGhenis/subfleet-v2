"""C-6.13: time-weighted samples, state-volume service latency and native faults."""
import ctypes
import math
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from subfleet import machine
REAL_READ = machine.read


def samples(monkeypatch, ticks, disks=None, times=None):
    ticks, disks = iter(ticks), iter(disks or [None] * 30)
    times = iter(times if times is not None else range(0, 300, 10))
    monkeypatch.setattr(machine, '_cpu_ticks', lambda: next(ticks))
    monkeypatch.setattr(machine, '_disk_counters', lambda *args: next(disks))
    monkeypatch.setattr(machine, '_sysctl_int', lambda _: 1)
    sampler = machine.Sampler(clock=lambda: next(times))
    monkeypatch.setattr(machine, '_state_device_id', lambda _: 1)
    return sampler


def test_cpu_ticks_are_interval_deltas_and_ewma_uses_sixty_seconds(monkeypatch):
    sampler = samples(monkeypatch, [((0, 0, 0, 0),), ((90, 0, 10, 0),), ((100, 0, 100, 0),)], times=[0, 10, 70])
    assert sampler.read()['cpu_idle'] is None
    assert sampler.read()['cpu_idle'] == pytest.approx(.1)
    a, b = -math.expm1(-10 / 60), -math.expm1(-1)
    assert sampler.read()['cpu_idle'] == pytest.approx((.1 * a * (1-b) + .9*b) / (a*(1-b)+b))


def test_short_first_sample_has_only_its_time_weight(monkeypatch):
    sampler = samples(monkeypatch, [((0, 0, 0, 0),), ((100, 0, 0, 0),), ((100, 0, 100, 0),)], times=[0, 1, 61])
    sampler.read(); sampler.read()
    assert sampler.read()['cpu_idle'] > .98


def test_lost_read_keeps_baseline_and_ewma(monkeypatch):
    sampler = samples(monkeypatch, [((0, 0, 0, 0),), ((100, 0, 0, 0),), None, ((110, 0, 90, 0),)], times=[0, 10, 20, 30])
    sampler.read()
    assert sampler.read()['cpu_idle'] == 0
    assert sampler.read()['cpu_idle'] is None
    a, b = -math.expm1(-10/60), -math.expm1(-20/60)
    assert sampler.read()['cpu_idle'] == pytest.approx(.9*b/(a*(1-b)+b))


def test_zero_ticks_keep_baseline_and_ewma(monkeypatch):
    sampler = samples(monkeypatch, [((0, 0, 0, 0),), ((100, 0, 0, 0),), ((100, 0, 0, 0),), ((110, 0, 90, 0),)])
    sampler.read(); sampler.read()
    assert sampler.read()['cpu_idle'] is None
    assert sampler.read()['cpu_idle'] < .9


def test_per_cpu_deltas_are_weighted_and_wrap_is_safe(monkeypatch):
    sampler = samples(monkeypatch, [((2**32-10, 0, 0, 0), (0, 0, 0, 0)), ((10, 0, 20, 0), (60, 0, 0, 0))])
    sampler.read()
    assert sampler.read()['cpu_idle'] == pytest.approx(.2)


@pytest.mark.parametrize('bad', [(), ((0,0,0,0),(0,0,0,0)), ((1,0,1,0),)])
def test_topology_or_reset_is_unreadable(monkeypatch, bad):
    sampler = samples(monkeypatch, [((100,0,100,0),), bad])
    sampler.read()
    assert sampler.read()['cpu_idle'] is None


def test_disk_measures_service_ms_on_state_device_without_cap(monkeypatch):
    # Unrelated device 2 must never choose the hold; 20 in flight is normal.
    sampler = samples(monkeypatch, [None]*3, [{1:(0,0,0,0),2:(0,0,0,0)},
        {1:(200_000_000_000,0,100_000,0),2:(9_000_000_000_000,0,1,0)},
        {1:(600_000_000_000,0,200_000,0),2:(99_000_000_000_000,0,2,0)}])
    sampler.read()
    row = sampler.read()
    assert row['disk_busy'] == pytest.approx(2)
    assert row['disk_in_flight'] == pytest.approx(20)
    assert 2 < sampler.read()['disk_busy'] < 4


def test_ordinary_counter_replay_holds_nothing_under_proposal(monkeypatch):
    from subfleet.policy import MACHINE_GUARD_PROPOSAL
    from subfleet.scheduler import MachineGuard, machine_hold
    # AC 90 s recording: elapsed seconds, service ms/op, uncapped in-flight.
    # These two independent rates reconstruct each interval's operation and
    # service counter deltas. CPU 33% idle is inside the measured idle range.
    recorded = [
        (0.0, 1.6621253968754017, 22.274210135078853),
        (1.0344894159934483, 2.096079956209779, 27.392165149207734),
        (2.133731207999517, 2.3389017291902783, 33.79278110888538),
        (3.224279916001251, 1.8728932166864374, 26.059617018000097),
        (4.4096006249892525, 2.1095936813427225, 26.032639557394546),
        (5.604983790995902, 1.3730762033260633, 21.066230715900776),
        (6.7189452500024345, 3.1018917760034155, 32.60718977602169),
        (7.808500457991613, 3.219189489294211, 29.80590917272963),
        (8.99921500000346, 1.7391027032300592, 22.156601836259018),
        (10.026087833000929, 2.603552749164537, 25.79535149710722),
        (11.217934000000241, 2.3044028034220534, 20.34013966167665),
        (12.218243082999834, 2.275563858358102, 22.19581621654538),
        (13.40143270799308, 2.3074599585880495, 19.778958846206557),
        (14.590450916002737, 2.6259768709643496, 21.000867688812725),
        (15.601235041001928, 1.670005703082614, 20.09887064462579),
        (16.694088249991182, 2.1226124122647816, 20.333590239948226),
        (17.823470749994158, 1.5916431189169138, 18.99741605961087),
        (18.91532025000197, 2.1362005424221855, 20.177356122654626),
        (19.995376207996742, 1.9728076042474787, 18.835683340675995),
        (21.124321665993193, 1.9317525481202316, 18.615191568507164),
        (22.28786145799677, 1.1301181247903855, 6.950450132070039),
        (23.387640916000237, 1.6007122954342714, 17.947583191663654),
        (24.404531082996982, 2.0643501019761175, 19.21047685286795),
        (25.5863455409999, 1.6080923770505116, 16.755633168054455),
        (26.735985874998732, 1.4566450216400089, 17.50674856195542),
        (27.901911082997685, 6.432744304114105, 76.97719194615836),
        (29.003539750003256, 1.884412212656949, 23.705069798140926),
        (30.103932415993768, 1.7554505517095522, 22.62925485202635),
        (31.110972624999704, 1.6854873895154554, 24.041086588685847),
        (32.21374875000038, 1.4457331412680248, 28.638669041715712),
        (33.40547324999352, 1.4846528564903723, 24.002868604417042),
        (34.44019283298985, 1.380152341317365, 24.279924224734692),
        (35.692010415994446, 1.0992733084716901, 20.62755018508573),
        (36.83264325000346, 1.341071733427164, 25.060600699641242),
        (37.90698745800182, 2.1139462826972557, 36.708702440419366),
        (39.12225254099758, 2.2153412352841606, 43.07762105856768),
        (40.19954758298991, 3.687854881502721, 58.49999267930237),
        (41.208484207993024, 2.762905487833498, 44.340709367014824),
        (42.40029525000136, 1.7397605412960608, 25.27432029933251),
        (43.42741279100301, 1.6759718799678844, 24.38774017389691),
        (44.42954224999994, 1.2070785023659307, 16.80055485830352),
        (45.50768604098994, 1.2784217405599951, 19.43819346560094),
        (46.708523415989475, 1.2325988779905666, 18.062431189742373),
        (47.93574579099368, 1.0347757967628246, 19.378926445110285),
        (48.98843274998944, 1.358420490909091, 17.104670532042235),
        (50.11069499999576, 1.3741287159378637, 17.892558469187577),
        (51.22882112499792, 1.459044528033209, 24.833581643525477),
        (52.43986670799495, 2.138182227561658, 27.7759014823825),
        (53.52490899999975, 1.5737369861410788, 17.477227222140485),
        (54.71413470800326, 1.9912634292175844, 21.10104210337676),
        (55.7377422080026, 2.5307378037649118, 22.59005655294127),
        (56.806588374995044, 2.628107206221564, 23.238368600685266),
        (58.08940179100318, 2.851291952926872, 25.592011383977997),
        (59.109704791000695, 2.1368200655159475, 27.9065408737105),
        (60.134755166000105, 2.4953762694444444, 24.53868942393635),
        (61.298712000003434, 1.5422718858546451, 18.340995834506124),
        (62.39762041599897, 1.3681930898575256, 17.652282342770555),
        (63.496220999993966, 1.5408685849259758, 10.42112461324904),
        (64.67847333299869, 1.5092249861706541, 9.230860088272182),
        (65.77559241600102, 1.1130180876539377, 9.80303227573811),
        (66.9109457500017, 1.4806979352304728, 8.799259816146352),
        (68.00076233300206, 2.783778522350066, 25.086321216302377),
        (69.20285758298996, 2.512430384692928, 26.953190474154173),
        (70.29001333299675, 2.4982077967795213, 27.827932123627313),
        (71.30606641600025, 2.772876650822669, 25.37759139490866),
        (72.39982270800101, 3.2301815113998327, 35.232771425257454),
        (73.44073762500193, 3.09065198996793, 36.10806054955098),
        (74.63152949999494, 3.163771817994614, 33.545562253886274),
        (75.73225237498991, 3.775907449907426, 33.350240157558),
        (76.87702074999106, 2.9172643680733947, 27.776956724514754),
        (78.08588308299659, 2.9871016460561233, 30.731856025023607),
        (79.09357199999795, 2.8681581506869884, 37.90951529235529),
        (80.21635000000242, 2.0004964955310998, 29.304248802406992),
        (81.39451258299232, 4.002703317068995, 39.688563212842176),
        (82.40287470800104, 3.199598163565556, 29.331809168997037),
        (83.42147799998929, 1.7032959265558316, 26.251675172583568),
        (84.44281316599518, 1.4866644638870887, 20.214039521166427),
        (85.60171812499175, 1.8509925610992968, 19.527257068253626),
        (86.8232643329975, 3.2786227008566406, 47.310762351225),
        (87.93014241599303, 1.5039228792067307, 20.34799440426795),
        (89.1239052079909, 1.6250223839302878, 21.245195876439464),
    ]
    service = operations = 0
    counters = [{1:(0,0,0,0)}]
    for previous, current in zip(recorded,recorded[1:]):
        elapsed=current[0]-previous[0]
        delta_service=round(current[2]*elapsed*1e9)
        service+=delta_service
        operations+=round(delta_service/(current[1]*1e6))
        counters.append({1:(service,0,operations,0)})
    sampler=samples(monkeypatch,[((i*67,0,i*33,0),) for i in range(len(recorded))],
                    counters,times=[r[0] for r in recorded])
    state=MachineGuard();chosen={'admission':{'machine_guard':MACHINE_GUARD_PROPOSAL}}
    sampler.read()
    for _,_,flight in recorded[1:]:
        row=sampler.read()
        decision=state.advance(chosen,row)
        assert all(machine_hold(chosen,decision,k) is None for k in ('background','session'))
        assert row['disk_in_flight']==pytest.approx(flight)


def test_reads_run_at_most_once_per_second(monkeypatch):
    calls=[]
    monkeypatch.setattr(machine, '_state_device_id', lambda _: 1)
    monkeypatch.setattr(machine,'_cpu_ticks',lambda: calls.append('cpu'))
    monkeypatch.setattr(machine,'_disk_counters',lambda *args: calls.append('disk'))
    times=iter([0,.05,.9,1,1.1,2])
    sampler=machine.Sampler(clock=lambda:next(times))
    for _ in range(6): sampler.read()
    assert calls == ['cpu','disk']*3


@pytest.mark.parametrize('source', ['_cpu_ticks','_disk_counters','_sysctl_int'])
def test_any_exception_is_fail_open(monkeypatch,source):
    def broken(*args): raise ctypes.ArgumentError('injected review fault')
    monkeypatch.setattr(machine,source,broken)
    row=machine.Sampler().read()
    assert row['cpu_idle' if source=='_cpu_ticks' else 'disk_busy' if source=='_disk_counters' else 'memory_pressure'] is None


def test_baseexception_is_not_swallowed(monkeypatch):
    def broken(): raise KeyboardInterrupt
    monkeypatch.setattr(machine,'_cpu_ticks',broken)
    with pytest.raises(KeyboardInterrupt): machine.Sampler().read()


def test_legacy_read_avoids_new_native_sources(monkeypatch):
    def forbidden(*args): pytest.fail('new native read in legacy mode')
    monkeypatch.setattr(machine,'_cpu_ticks',forbidden)
    monkeypatch.setattr(machine,'_disk_counters',forbidden)
    monkeypatch.setattr(machine,'_root_device_id',forbidden)
    monkeypatch.setattr(machine,'_state_device_id',forbidden)
    assert 'load1' in REAL_READ()


def test_a_read_starts_no_subprocess_even_on_first_library_load():
    code = '''import sys
from subfleet import machine
REAL_READ = machine.read
seen=[]
def audit(event,args):
    if event in ('subprocess.Popen','os.system','os.posix_spawn','os.fork','os.forkpty') or event.startswith('os.exec'):
        seen.append(event)
sys.addaudithook(audit)
s=machine.Sampler()
s.read(); s.read()
assert not seen, seen
'''
    subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[2],check=True,timeout=30)


class Registry:
    """Minimal CF/IO registry; counts releases and can hide a device's fields."""
    def __init__(self, count, bad=None):
        self.entries=iter(range(1,count+1)); self.released=[]; self.bad=bad
        self.io=self.cf=self
    def CFStringCreateWithCString(self,_,name,encoding): return name
    def IOServiceMatching(self,_): return 99
    def IOServiceGetMatchingServices(self,_,matching,out): out._obj.value=9999; return 0
    def IOIteratorNext(self,_): return next(self.entries,0)
    def IOObjectRelease(self,obj): self.released.append(obj)
    def IORegistryEntryInPlane(self,obj,plane): return True
    def IORegistryEntryGetRegistryEntryID(self,obj,out):
        out._obj.value=obj
        return int(self.bad==('id',obj))
    def IORegistryEntryCreateCFProperty(self,obj,*args):
        if self.bad==('exception',obj): raise ctypes.ArgumentError('bad device')
        return None if self.bad==('stats',obj) else {'device':obj}
    def CFGetTypeID(self,obj): return 1 if isinstance(obj,dict) else 2
    def CFDictionaryGetTypeID(self): return 1
    def CFNumberGetTypeID(self): return 2
    def CFDictionaryGetValue(self,obj,key): return 100000 if b'Time' in key else 100
    def CFNumberGetValue(self,value,kind,out): out._obj.value=value; return True
    def CFRelease(self,_): pass


@pytest.mark.parametrize('count',[1024,1025])
def test_device_enumeration_includes_the_stated_limit(count):
    registry=Registry(count)
    storage=object.__new__(machine._Storage); storage.io=registry; storage.cf=registry
    result=storage.read()
    assert len(result)==1024
    assert 1024 in result and 1025 not in result
    assert len(registry.released)==1025  # 1024 devices plus iterator


@pytest.mark.parametrize('failure',['id','stats','exception'])
def test_unreadable_device_does_not_hide_healthy_neighbor(failure):
    registry=Registry(2,(failure,1))
    storage=object.__new__(machine._Storage); storage.io=registry; storage.cf=registry
    assert set(storage.read()) == {2}
    assert 1 in registry.released


@pytest.mark.skipif(sys.platform!='darwin',reason='Mach/IOKit boundary')
@pytest.mark.parametrize('source',['cpu','disk'])
def test_review_argumenterror_at_native_call_is_fail_open(monkeypatch,source):
    def injected(*args): raise ctypes.ArgumentError('review injection')
    if source=='cpu':
        monkeypatch.setattr(machine._system(),'host_processor_info',injected)
        assert machine._cpu_ticks() is None
    else:
        storage=machine._Storage()
        monkeypatch.setattr(storage.io,'IORegistryEntryCreateCFProperty',injected)
        monkeypatch.setattr(machine,'_iokit',storage)
        assert machine._disk_counters() is None


def test_restored_baseline_age_is_the_calibration_sample_age(monkeypatch,tmp_path):
    monkeypatch.setattr(machine.time,'time',lambda:2000.0)
    sampler=machine.Sampler(tmp_path,history=[{'observed_at':1000.0,'disk_service_ms_raw':2.0,'disk_device_id':1}])
    sampler.device_id=1
    baseline=sampler.status()['disk_baseline_ms']
    assert baseline['value']==2
    assert baseline['age_s']==1000
