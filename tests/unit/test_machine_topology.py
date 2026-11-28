"""C-6.13 round two: topology continuity and calibration eligibility."""
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import machine, scheduler

CI = settings(max_examples=40, deadline=None, derandomize=True, database=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])


class Source:
    def __init__(self, monkeypatch, tmp_path):
        self.now = 0
        self.root_device = 10
        self.device = 1
        self.disks = {1: (0, 0, 0, 0)}
        self.resolutions = 0
        monkeypatch.setattr(machine, '_root_device_id', lambda _: self.root_device, raising=False)
        monkeypatch.setattr(machine, '_state_device_id', self.resolve)
        monkeypatch.setattr(machine, '_cpu_ticks', lambda: None)
        monkeypatch.setattr(machine, '_disk_counters', lambda *args: self.disks)
        monkeypatch.setattr(machine, '_legacy_read', lambda: {})
        monkeypatch.setattr(machine.time, 'time', lambda: self.now)
        self.sampler = machine.Sampler(tmp_path, clock=lambda: self.now)

    def resolve(self, _):
        self.resolutions += 1
        return self.device

    def read(self, root, device, disks):
        self.now += 1
        self.root_device, self.device, self.disks = root, device, disks
        return self.sampler.read()


def test_review_four_step_device_replacement_resumes_without_recreation(monkeypatch, tmp_path):
    source = Source(monkeypatch, tmp_path)
    assert source.read(10, 1, {1: (0, 0, 0, 0)})['disk_busy'] is None
    assert source.read(10, 1, {1: (100_000_000, 0, 100, 0)})['disk_busy'] == 1
    row = source.read(20, 2, {2: (0, 0, 0, 0)})
    assert row['disk_device_id'] == 2
    assert row['disk_busy'] is None and row['disk_in_flight'] is None
    assert row['disk_baseline_ms'] is None
    assert source.read(20, 2, {2: (200_000_000, 0, 100, 0)})['disk_busy'] == 2
    assert source.resolutions == 2


def test_root_identity_change_ignores_still_readable_old_device_and_ewma(monkeypatch, tmp_path):
    source = Source(monkeypatch, tmp_path)
    source.read(10, 1, {1: (0, 0, 0, 0)})
    source.read(10, 1, {1: (1_000_000_000, 0, 100, 0)})
    # Increasing counters would produce a plausible, but cross-device, delta.
    row = source.read(20, 2, {1: (2_000_000_000, 0, 200, 0), 2: (8_000_000_000, 0, 300, 0)})
    assert row['disk_busy'] is None
    row = source.read(20, 2, {2: (8_100_000_000, 0, 400, 0)})
    assert row['disk_busy'] == pytest.approx(1)
    assert row['disk_in_flight'] == pytest.approx(.1)


def test_failed_cached_read_reresolves_even_when_root_stat_is_unchanged(monkeypatch, tmp_path):
    source = Source(monkeypatch, tmp_path)
    source.read(10, 1, {1: (0, 0, 0, 0)})
    assert source.read(10, 2, {2: (100_000_000, 0, 100, 0)})['disk_busy'] is None
    assert source.read(10, 2, {2: (300_000_000, 0, 200, 0)})['disk_busy'] == 2
    assert source.resolutions == 2


def test_unreadable_topology_does_not_hold_or_release_disk_latch(monkeypatch, tmp_path):
    source = Source(monkeypatch, tmp_path)
    chosen = {'admission': {'machine_guard': {'background': {'disk_busy_max': 4}}}}
    guard = scheduler.MachineGuard()
    def hold(row):
        return scheduler.machine_hold(chosen, guard.advance(chosen, row), 'background')
    source.read(10, 1, {1: (0, 0, 0, 0)})
    assert hold(source.read(10, 1, {1: (1_000_000_000, 0, 100, 0)}))
    assert hold(source.read(None, None, None)) is None
    assert hold(source.read(20, 2, {2: (0, 0, 0, 0)})) is None
    assert hold(source.read(20, 2, {2: (100_000_000, 0, 100, 0)}))  # latch survives


@CI
@given(st.lists(st.tuples(st.sampled_from([None, 10, 20, 30]),
                         st.sampled_from([None, 1, 2]), st.booleans(),
                         st.integers(1, 20)), max_size=25))
def test_generated_topology_appear_disappear_replace_return_and_reused_id(monkeypatch, tmp_path, events):
    source = Source(monkeypatch, tmp_path)
    # Every example covers disappearance, return and a registry ID reused on a
    # different root device, followed by arbitrary bounded topology changes.
    events = [(10, 1, True, 10), (None, None, False, 1), (10, 1, True, 3),
              (20, 2, True, 8), (30, 1, True, 2), (10, 1, True, 5)] + events
    for root, device, readable, service in events:
        available = root is not None and device is not None and readable
        # Separate monotone epochs catch a retained cross-device baseline even
        # when the reused device's counters exceed the old device's counters.
        base = int(source.now + 1) * 1000
        counters = {device: (base * 1_000_000_000, 0, base, 0)} if available else None
        # Unknown samples retain the last good root and resolved device. The
        # former oracle incorrectly expected a reset after an unreadable stat.
        previous = (source.sampler._root_device, source.sampler.device_id)
        row = source.read(root, device, counters)
        if not available:
            assert row['disk_busy'] is None
            continue
        if previous != (root, device):
            assert row['disk_busy'] is None
        counters = {device: (base * 1_000_000_000 + service * 100_000_000, 0, base + 100, 0)}
        row = source.read(root, device, counters)
        assert row['disk_device_id'] == device
        assert row['disk_service_ms_raw'] == service
        # New identity epochs start with exactly their own service time.
        if previous != (root, device):
            assert row['disk_busy'] == pytest.approx(service)


def test_review_zero_service_observation_does_not_refresh_baseline_age(monkeypatch, tmp_path):
    source = Source(monkeypatch, tmp_path)
    source.now = 998
    source.read(10, 1, {1: (0, 0, 0, 0)})
    source.sampler.record(source.read(10, 1, {1: (2_000_000, 0, 1, 0)}))
    source.now = 1998
    zero = source.read(10, 1, {1: (2_000_000, 0, 2, 0)})
    assert zero['disk_service_ms_raw'] == 0
    source.sampler.record(zero)
    source.now = 2000
    baseline = source.sampler.status()['disk_baseline_ms']
    assert baseline['value'] == 2
    assert baseline['age_s'] == 1000


@CI
@given(st.lists(st.tuples(st.integers(-90000, 0), st.sampled_from([1, 2]),
                         st.one_of(st.none(), st.sampled_from([0, -1, float('nan'), float('inf')]),
                                   st.integers(1, 100))), max_size=40))
def test_generated_baseline_age_uses_newest_eligible_observation(monkeypatch, tmp_path, observations):
    now = 100000
    monkeypatch.setattr(machine.time, 'time', lambda: now)
    rows = [{'observed_at': now+dt, 'disk_device_id': device, 'disk_service_ms_raw': value}
            for dt, device, value in observations]
    rows += [{'observed_at': 99000, 'disk_device_id': 1, 'disk_service_ms_raw': 2},
             {'observed_at': 99999, 'disk_device_id': 1, 'disk_service_ms_raw': 0}]
    sampler = machine.Sampler(tmp_path, history=rows)
    sampler.device_id = 1
    eligible = [r for r in rows if r['disk_device_id'] == 1 and r['observed_at'] >= 13600
                and isinstance(r['disk_service_ms_raw'], int) and r['disk_service_ms_raw'] > 0]
    baseline = sampler.status()['disk_baseline_ms']
    assert baseline['age_s'] == now-max(r['observed_at'] for r in eligible)
    values = sorted(r['disk_service_ms_raw'] for r in eligible)
    assert baseline['value'] == values[int((len(values)-1)*.1)]


def test_selected_device_read_never_enumerates_neighbors():
    from tests.unit.test_machine_signals import Registry
    registry = Registry(0)
    registry.IORegistryEntryIDMatching = lambda identity: identity
    registry.IOServiceGetMatchingService = lambda port, matching: matching
    def enumerate_forbidden(*args):
        pytest.fail('steady selected-device read enumerated IOKit devices')
    registry.IOServiceGetMatchingServices = enumerate_forbidden
    storage = object.__new__(machine._Storage)
    storage.io = storage.cf = registry
    assert storage.read(42) == {42: (100000, 100000, 100, 100)}
    storage.close()
    assert registry.released == [42]


def test_unresolved_root_has_no_baseline_from_other_devices(monkeypatch, tmp_path):
    monkeypatch.setattr(machine.time, 'time', lambda: 2000)
    sampler = machine.Sampler(tmp_path, history=[
        {'observed_at': 1000, 'disk_service_ms_raw': 2, 'disk_device_id': 1}])
    assert sampler.status()['disk_baseline_ms']['readable'] is False


def test_reused_registry_id_on_new_root_discards_counter_baseline(monkeypatch, tmp_path):
    source = Source(monkeypatch, tmp_path)
    source.read(10, 1, {1: (0, 0, 0, 0)})
    old = source.read(10, 1, {1: (1_000_000_000, 0, 100, 0)})
    source.sampler.record(old)
    changed = source.read(20, 1, {1: (8_000_000_000, 0, 300, 0)})
    assert changed['disk_busy'] is None
    assert changed['disk_baseline_ms'] is None
    assert source.sampler.status()['disk_busy']['value'] is None
    assert source.read(20, 1, {1: (8_100_000_000, 0, 400, 0)})['disk_busy'] == 1


def test_baseline_value_and_age_share_one_cutoff_at_window_boundary(monkeypatch, tmp_path):
    wall = iter([100000.0, 100000.5])
    monkeypatch.setattr(machine.time, 'time', lambda: next(wall))
    sampler = machine.Sampler(tmp_path, history=[
        {'observed_at': 13600, 'disk_service_ms_raw': 2, 'disk_device_id': 1}])
    sampler.device_id = 1
    baseline = sampler.status()['disk_baseline_ms']
    assert baseline['value'] == 2
    assert baseline['age_s'] == 86400


def test_status_keeps_value_and_age_together_during_device_eviction(monkeypatch, tmp_path):
    class Evicting(dict):
        def get(self, key, default=None):
            # Model a device change clearing the entry just after it was read.
            value = super().get(key, default)
            self.pop(key, None)
            return value
    monkeypatch.setattr(machine.time, 'time', lambda: 2000)
    sampler = machine.Sampler(tmp_path)
    sampler._latest = Evicting(disk_busy=(4, 1000))
    sampler._snapshot = {'disk_busy': 4}
    view = sampler.status()['disk_busy']
    assert view['value'] == 4
    assert view['age_s'] == 1000
