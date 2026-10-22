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
    monkeypatch.setattr(machine, '_disk_counters', lambda: next(disks))
    monkeypatch.setattr(machine, '_sysctl_int', lambda _: 1)
    sampler = machine.Sampler(clock=lambda: next(times))
    sampler.device_id = 1
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
    sampler = samples(monkeypatch, [((i*50,0,i*50,0),) for i in range(10)],
        [{1:(i*200_000_000_000,0,i*100_000,0)} for i in range(10)])
    state = MachineGuard(); chosen = {'admission':{'machine_guard':MACHINE_GUARD_PROPOSAL}}
    sampler.read()
    for _ in range(9):
        row = sampler.read()
        assert row['disk_in_flight'] == 20
        row = state.advance(chosen, row)
        assert all(machine_hold(chosen,row,k) is None for k in ('background','session'))


def test_reads_run_at_most_once_per_second(monkeypatch):
    calls=[]
    monkeypatch.setattr(machine,'_cpu_ticks',lambda: calls.append('cpu'))
    monkeypatch.setattr(machine,'_disk_counters',lambda: calls.append('disk'))
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
    def forbidden(): pytest.fail('new native read in legacy mode')
    monkeypatch.setattr(machine,'_cpu_ticks',forbidden)
    monkeypatch.setattr(machine,'_disk_counters',forbidden)
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
