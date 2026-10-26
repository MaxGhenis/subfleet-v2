"""Fixed-seed state-machine properties: 3,000 bounded topology sequences."""
import math
import random

import pytest

from subfleet import machine, scheduler
from tests.unit.test_machine_topology import Source


@pytest.mark.parametrize('seed', [241, 613, 20261023])
def test_generated_unknown_is_not_change_and_readings_use_one_device(monkeypatch, tmp_path, seed):
    rng = random.Random(seed)
    chosen = {'admission': {'machine_guard': {'background': {'disk_busy_max': 4}}}}
    for sequence in range(1000):
        source = Source(monkeypatch, tmp_path)
        root_key = identity = baseline = disk_at = None
        total = weight = 0.0
        counters = {1: [0, 0, 0, 0], 2: [0, 0, 0, 0]}
        guard = scheduler.MachineGuard()
        # All sequences exercise faults, same-device return, a changed root
        # reusing its ID, and a replacement while the root's key stays stable.
        events = [(10, 1, True), (10, 1, True), (None, None, True),
                  (10, 1, True), (20, None, True), (20, 1, True),
                  (20, 1, True), (20, None, False), (20, 2, True),
                  (20, 2, True)]
        events += [(rng.choice([None, 10, 20]), rng.choice([None, 1, 2]),
                    rng.choice([True, True, False])) for _ in range(15)]
        for root, device, readable in events:
            service = rng.choice([1, 2, 5, 10, 20])
            source.now += rng.randint(1, 60)
            now = source.now + 1  # Source.read advances the clock once
            for values in counters.values():
                values[0] += service * 100_000_000
                values[2] += 100
            # Include a healthy unrelated driver. Resolution, not the busiest
            # available device, determines which counters may be consumed.
            available = {key: tuple(value) for key, value in counters.items()
                         if readable and (key == device or key != identity)}
            current = None
            changed = False
            old_state = (root_key, identity, baseline, disk_at, total, weight)
            if root is not None:
                if root != root_key or identity is None:
                    if device is not None:
                        changed = root != root_key or device != identity
                        root_key, identity = root, device
                        current = available.get(identity)
                else:
                    current = available.get(identity)
                    if current is None and device is not None and device != identity:
                        changed = True
                        identity = device
                        current = available.get(identity)
            if changed:
                baseline = disk_at = None
                total = weight = 0.0
            expected = raw = None
            if current is not None:
                if baseline is not None:
                    delta = tuple(a-b for a, b in zip(current, baseline))
                    raw = sum(delta[:2]) / sum(delta[2:]) / 1e6
                    elapsed = now - disk_at
                    decay = math.exp(-elapsed / 60)
                    total = total * decay + raw * (1-decay)
                    weight = weight * decay + 1-decay
                    expected = total / weight
                baseline, disk_at = current, now
            row = source.read(root, device, available)
            context = seed, sequence, now, root, device, readable
            assert source.sampler.device_id == identity, context
            assert source.sampler._root_device == root_key, context
            assert source.sampler._disk == baseline, context
            assert source.sampler._disk_at == disk_at, context
            assert source.sampler._disk_ewma.total == pytest.approx(total), context
            assert source.sampler._disk_ewma.weight == pytest.approx(weight), context
            if expected is None:
                assert row['disk_busy'] is None, context
            else:
                assert row['disk_busy'] == pytest.approx(expected), context
                assert row['disk_service_ms_raw'] == pytest.approx(raw), context
                assert source.sampler._disk_id == row['disk_device_id'] == identity, context
            if current is None and not changed:
                assert (root_key, identity, baseline, disk_at, total, weight) == old_state, context
            latched = set(guard._latched)
            decision = guard.advance(chosen, row)
            if row['disk_busy'] is None:
                assert scheduler.machine_hold(chosen, decision, 'background') is None, context
                assert set(guard._latched) == latched, context
            # Every calibration percentile and timestamp must be selected from
            # just the current device, including interleaved neighbors.
            source.sampler.record(row)
            source.sampler.record({'observed_at': now, 'disk_device_id': 3,
                                   'disk_service_ms_raw': .001})
            eligible = [r for r in source.sampler._history
                        if r.get('disk_device_id') == identity and identity is not None
                        and r.get('disk_service_ms_raw') is not None
                        and r['disk_service_ms_raw'] > 0]
            view = source.sampler.status()['disk_baseline_ms']
            values = sorted(r['disk_service_ms_raw'] for r in eligible)
            assert view['value'] == (values[int((len(values)-1)*.1)] if values else None), context
            assert view['age_s'] == (now-max(r['observed_at'] for r in eligible) if values else None), context
