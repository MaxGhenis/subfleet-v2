"""Mach/IOKit deltas, smoothing, missing data and the no-subprocess boundary."""

import math
import subprocess

import pytest

from subfleet import machine


def samples(monkeypatch, ticks, disks=None, times=None):
    ticks = iter(ticks)
    disks = iter(disks if disks is not None else [None] * 20)
    times = iter(times if times is not None else range(0, 1200, 10))
    monkeypatch.setattr(machine, "_cpu_ticks", lambda: next(ticks))
    monkeypatch.setattr(machine, "_disk_counters", lambda: next(disks))
    monkeypatch.setattr(machine.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(machine, "_sysctl_int", lambda _: 1)
    return machine.Sampler()


def test_cpu_ticks_are_interval_deltas_and_ewma_uses_sixty_seconds(monkeypatch):
    sampler = samples(monkeypatch, [((100, 0, 100, 0),), ((190, 0, 110, 0),),
                                    ((200, 0, 200, 0),), ((290, 0, 210, 0),)], times=[0, 10, 70, 100])
    assert sampler.read()["cpu_idle"] is None  # no since-boot utilization
    assert sampler.read()["cpu_idle"] == pytest.approx(.1)
    smooth = .1 + (1 - math.exp(-1)) * .8
    assert sampler.read()["cpu_idle"] == pytest.approx(smooth)
    assert sampler.read()["cpu_idle"] == pytest.approx(smooth + (1 - math.exp(-.5)) * (.1 - smooth))


def test_per_cpu_deltas_are_weighted_by_ticks_and_unsigned_wrap_is_safe(monkeypatch):
    sampler = samples(monkeypatch, [((2**32 - 10, 0, 0, 0), (0, 0, 0, 0)),
                                    ((10, 0, 20, 0), (60, 0, 0, 0))])
    sampler.read()
    assert sampler.read()["cpu_idle"] == pytest.approx(.2)


@pytest.mark.parametrize("bad", [None, (), ((0, 0, 0, 0), (0, 0, 0, 0)), ((1, 0, 1, 0),)])
def test_cpu_failure_topology_change_and_counter_reset_hold_nothing(monkeypatch, bad):
    sampler = samples(monkeypatch, [((100, 0, 100, 0),), ((200, 0, 100, 0),), bad,
                                    ((300, 0, 300, 0),), ((350, 0, 350, 0),)])
    sampler.read()
    assert sampler.read()["cpu_idle"] == 0
    assert sampler.read()["cpu_idle"] is None
    sampler.read()  # rebaseline after unavailable/reset/topology change
    assert sampler.read()["cpu_idle"] == .5


def test_zero_interval_and_zero_tick_delta_are_unreadable(monkeypatch):
    sampler = samples(monkeypatch, [((0, 0, 0, 0),), ((1, 0, 0, 0),), ((1, 0, 0, 0),)], times=[0, 0, 10])
    assert all(sampler.read()["cpu_idle"] is None for _ in range(3))


def test_disk_busy_is_bounded_service_time_per_wall_second_on_busiest_device(monkeypatch):
    sampler = samples(monkeypatch, [None] * 3,
                      disks=[{1: (0, 0), 2: (0, 0)},
                             {1: (2_000_000_000, 3_000_000_000), 2: (1_000_000_000, 0)},
                             {1: (52_000_000_000, 3_000_000_000), 2: (1_000_000_000, 0)}])
    assert sampler.read()["disk_busy"] is None
    assert sampler.read()["disk_busy"] == .5
    assert sampler.read()["disk_busy"] == 1


@pytest.mark.parametrize("bad", [None, {}, {2: (10, 10)}, {1: (1, 1)}])
def test_disk_missing_reset_or_device_change_is_unreadable(monkeypatch, bad):
    sampler = samples(monkeypatch, [None] * 3, disks=[{1: (10, 10)}, bad, {1: (20, 20)}])
    sampler.read()
    assert sampler.read()["disk_busy"] is None


def test_a_read_starts_no_subprocess_even_on_first_library_load(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("machine pass attempted a subprocess")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(machine.os, "system", forbidden)
    monkeypatch.setattr(machine, "_libc", None)
    monkeypatch.setattr(machine, "_iokit", None)
    sampler = machine.Sampler()
    first, second = sampler.read(), sampler.read()
    for reading in (first, second):
        assert reading["read_cost_s"] >= 0
        for signal in ("cpu_idle", "disk_busy"):
            assert reading[signal] is None or 0 <= reading[signal] <= 1


def test_each_signal_is_read_once_per_pass(monkeypatch):
    calls = []
    monkeypatch.setattr(machine, "_cpu_ticks", lambda: calls.append("cpu"))
    monkeypatch.setattr(machine, "_disk_counters", lambda: calls.append("disk"))
    machine.Sampler().read()
    assert calls == ["cpu", "disk"]
