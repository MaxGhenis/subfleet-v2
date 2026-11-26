"""Review r3: an unknown topology sample is not a device transition."""
import pytest

from subfleet import machine, scheduler
from tests.unit.test_machine_topology import Source


@pytest.mark.parametrize('fault', ['stat', 'lookup'])
def test_unknown_sample_preserves_counter_baseline_and_smoothing(monkeypatch, tmp_path, fault):
    source = Source(monkeypatch, tmp_path)
    source.now = -1
    source.read(10, 1, {1: (0, 0, 0, 0)})
    source.now = 0
    assert source.read(10, 1, {1: (1_000_000_000, 0, 100, 0)})['disk_busy'] == 10
    previous = (source.sampler._disk, source.sampler._disk_at, source.sampler._disk_ewma)
    source.now = 1
    assert source.read(None if fault == 'stat' else 10, None, None)['disk_busy'] is None
    assert source.sampler.device_id == 1
    assert (source.sampler._disk, source.sampler._disk_at, source.sampler._disk_ewma) == previous
    source.now = 2
    row = source.read(10, 1, {1: (1_200_000_000, 0, 200, 0)})
    assert row['disk_busy'] == pytest.approx(4.6223, abs=.0001)
    assert row['disk_service_ms_raw'] == 2


@pytest.mark.parametrize('fault', ['none', 'stat', 'lookup'])
def test_review_three_row_hold_table_keeps_ewma_after_unknown(monkeypatch, tmp_path, fault):
    source = Source(monkeypatch, tmp_path)
    source.now = -1
    source.read(10, 1, {1: (0, 0, 0, 0)})
    chosen = {'admission': {'machine_guard': {'background': {'disk_busy_max': 4}}}}
    guard = scheduler.MachineGuard()
    rows = {}
    released_at = None
    for tick in range(1, 241):
        operations = tick * 100
        service = (min(tick, 120) * 1000 + max(tick - 120, 0) * 200) * 1_000_000
        source.now = tick - 1
        root, device, counters = 10, 1, {1: (service, 0, operations, 0)}
        if tick == 121 and fault != 'none':
            root = None if fault == 'stat' else 10
            device, counters = None, None
        row = source.read(root, device, counters)
        hold = scheduler.machine_hold(chosen, guard.advance(chosen, row), 'background')
        if tick > 120 and row['disk_busy'] is not None and hold is None and released_at is None:
            released_at = tick
        if tick in (121, 122, 180):
            rows[tick] = (row['disk_busy'], hold is not None)
    if fault == 'none':
        assert rows[121][0] == pytest.approx(9.8474701540)
    else:
        assert rows[121] == (None, False)
    assert rows[122][0] == pytest.approx(9.6982272087)
    assert rows[122][1] is True
    assert rows[180][0] == pytest.approx(4.6780723538)
    assert rows[180][1] is True
    assert released_at == 220


def test_failed_stat_reads_no_disk_counters_and_keeps_last_good_root(monkeypatch, tmp_path):
    source = Source(monkeypatch, tmp_path)
    source.read(10, 1, {1: (0, 0, 0, 0)})
    def forbidden(*args):
        pytest.fail('unknown root must not read cached or neighboring counters')
    monkeypatch.setattr(machine, '_disk_counters', forbidden)
    row = source.read(None, None, {1: (1_000_000, 0, 1, 0)})
    assert row['disk_busy'] is None
    assert source.sampler._root_device == 10
    assert source.sampler.device_id == 1


def test_root_change_is_remembered_until_lookup_recovers(monkeypatch, tmp_path):
    source = Source(monkeypatch, tmp_path)
    source.read(10, 1, {1: (0, 0, 0, 0)})
    source.read(10, 1, {1: (1_000_000_000, 0, 100, 0)})
    source.read(20, None, None)
    assert source.sampler._root_device == 10
    assert source.sampler.device_id == 1
    assert source.read(20, 1, {1: (2_000_000_000, 0, 200, 0)})['disk_busy'] is None
    assert source.read(20, 1, {1: (2_100_000_000, 0, 300, 0)})['disk_busy'] == 1


def test_status_baseline_captures_device_once_before_history_scan(monkeypatch, tmp_path):
    source = Source(monkeypatch, tmp_path)
    source.sampler.device_id = 1
    class SwitchingHistory(list):
        def __iter__(self):
            yield self[0]
            source.sampler.device_id = 2  # admission changes device between rows
            yield self[1]
    source.sampler._history = SwitchingHistory([
        {'observed_at': 1000, 'disk_device_id': 1, 'disk_service_ms_raw': 2.0},
        {'observed_at': 1900, 'disk_device_id': 2, 'disk_service_ms_raw': 50.0},
    ])
    source.now = 2000
    baseline = source.sampler.status()['disk_baseline_ms']
    assert baseline['value'] == 2.0
    assert baseline['age_s'] == 1000


class HandleRegistry:
    """Extend the existing fake with retained handles and detached stale data."""
    def __init__(self):
        from tests.unit.test_machine_signals import Registry
        self.registry = Registry(0)
        self.handles = {42: 142, 43: 143}
        self.attached = {142, 143}
        self.lookups, self.stats = [], []

    def __getattr__(self, key):
        return getattr(self.registry, key)

    def IORegistryEntryIDMatching(self, identity):
        self.lookups.append(identity)
        return identity

    def IOServiceGetMatchingService(self, port, matching):
        return self.handles.get(matching, 0)

    def IORegistryEntryGetRegistryEntryID(self, handle, out):
        out._obj.value = handle - 100
        return 0

    def IORegistryEntryInPlane(self, handle, plane):
        assert plane == b'IOService'
        return handle in self.attached

    def IORegistryEntryCreateCFProperty(self, handle, *args):
        self.stats.append(handle)
        return self.registry.IORegistryEntryCreateCFProperty(handle, *args)

    def storage(self):
        storage = object.__new__(machine._Storage)
        storage.io = storage.cf = self
        return storage


def test_selected_handle_is_retained_for_steady_reads_and_released_on_close():
    registry = HandleRegistry()
    storage = registry.storage()
    for _ in range(2):
        assert storage.read(42) == {42: (100000, 100000, 100, 100)}
    assert registry.lookups == [42]
    assert registry.released == []
    storage.close()
    storage.close()
    assert registry.released == [142]


def test_disappeared_handle_is_unknown_then_reresolved_by_id():
    registry = HandleRegistry()
    storage = registry.storage()
    assert storage.read(42) is not None
    registry.attached.remove(142)
    registry.handles.pop(42)
    # The fake deliberately still returns the old statistics and ID for 142.
    # A retained object is not evidence that its device is still attached.
    assert storage.read(42) is None
    assert registry.stats == [142]
    assert registry.released == [142]
    assert registry.lookups == [42]  # do not retry inside this failed read
    assert storage.read(42) is None
    assert registry.lookups == [42, 42]
    assert storage.read(43) == {43: (100000, 100000, 100, 100)}
    storage.close()


def test_replacement_handle_never_returns_another_devices_counters():
    registry = HandleRegistry()
    storage = registry.storage()
    registry.handles[42] = 143
    assert storage.read(42) is None
    assert registry.stats == []
    assert registry.released == [143]
    storage.close()


def test_ancestry_resolution_retains_driver_for_first_counter_read(monkeypatch, tmp_path):
    from types import SimpleNamespace
    registry = HandleRegistry()
    registry.IOBSDNameMatching = lambda *args: 42
    registry.IOObjectConformsTo = lambda *args: True
    storage = registry.storage()
    monkeypatch.setattr(machine, '_system', lambda: SimpleNamespace(devname=lambda *args: b'disk1'))
    assert storage.device_id(tmp_path) == 42
    assert registry.released == []
    assert storage.read(42) is not None
    assert registry.lookups == []
    storage.close()


def test_handle_detached_during_statistics_read_is_unknown():
    registry = HandleRegistry()
    storage = registry.storage()
    original = registry.IORegistryEntryCreateCFProperty
    def detach(handle, *args):
        result = original(handle, *args)
        registry.attached.remove(handle)
        return result
    registry.IORegistryEntryCreateCFProperty = detach
    assert storage.read(42) is None
    assert registry.released == [142]
    storage.close()
