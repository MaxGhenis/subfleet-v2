"""Named regressions reject each review-r3 mutant, with positive controls."""
from pathlib import Path
from types import ModuleType

import pytest

from subfleet import machine
from tests.unit import test_machine_device_continuity as regressions
from tests.unit import test_machine_topology as topology


def replace_once(source, old, new):
    assert source.count(old) == 1, old
    return source.replace(old, new)


@pytest.mark.parametrize('mutation', ['unknown_change', 'device_per_row', 'stale_handle'])
@pytest.mark.parametrize('mutated', [False, True], ids=['positive_control', 'mutant'])
def test_review_mutation_fails_named_regression(monkeypatch, tmp_path, mutation, mutated):
    source = Path(machine.__file__).read_text()
    if mutated:
        if mutation == 'unknown_change':
            source = replace_once(source,
                '        if identity is None:\n            return  # unknown is not evidence of a device transition\n', '')
        elif mutation == 'device_per_row':
            source = replace_once(source, 'now - HISTORY_WINDOW_S, identity)',
                                  'now - HISTORY_WINDOW_S, self.device_id)')
        else:
            source = replace_once(source,
                'if not io.IORegistryEntryInPlane(selected, b"IOService"):', 'if False:')
            source = replace_once(source,
                'if len(counters) == 4 and (device_id is None or\n'
                '                            io.IORegistryEntryInPlane(device, b"IOService")):',
                'if len(counters) == 4:')
    module = ModuleType('review_machine_copy')
    exec(compile(source, '<review_machine_copy>', 'exec'), module.__dict__)
    monkeypatch.setattr(regressions, 'machine', module)
    monkeypatch.setattr(topology, 'machine', module)

    def run_named_regression():
        if mutation == 'unknown_change':
            regressions.test_unknown_sample_preserves_counter_baseline_and_smoothing(
                monkeypatch, tmp_path, 'lookup')
        elif mutation == 'device_per_row':
            regressions.test_status_baseline_captures_device_once_before_history_scan(
                monkeypatch, tmp_path)
        else:
            regressions.test_disappeared_handle_is_unknown_then_reresolved_by_id()

    if mutated:
        with pytest.raises(AssertionError):
            run_named_regression()
    else:
        run_named_regression()
