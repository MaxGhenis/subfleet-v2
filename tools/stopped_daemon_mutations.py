"""One pytest process, five isolated review mutations, no source-file writes.

Run under the shared suite lock with the normal scratch/Hypothesis environment:
    uv run python tools/stopped_daemon_mutations.py -q -p no:cacheprovider

Pytest must fail all seven named cases. This command succeeds only when every
mutation is killed, and restores each function before the next case. Optional
normal pytest selections are left unmodified; any failure among them also
makes this command fail.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest
from subfleet import client, hooks

CASES = {
    'test_settlement_probe_wait_keeps_unknown_outcome': 'settlement',
    'test_busy_then_refused_diagnoses_share_original_call_ceiling': 'shared-ceiling',
    'test_late_ps_observation_cannot_verify_stopped_holder': 'late-observation',
    'test_terminal_poll_keeps_delivery_reserve': 'notice-split',
    'test_slow_boot_lookup_paces_actual_ps_starts': 'before-boot',
    'test_probe_bucket_rate_property': 'before-boot',
}


def changed(source, old, new):
    assert source.count(old) == 1, old
    return source.replace(old, new)


def replacement(name):
    module = hooks if name == 'notice-split' else client
    source = Path(module.__file__).read_text()
    if name == 'settlement':
        source = changed(source, 'except (ResponseLost, RequestTimeout) as exc:', 'except ResponseLost as exc:')
        owner, member = module.Client, 'call_settled'
    elif name == 'shared-ceiling':
        source = changed(source, '        if deadline_at is not None:\n            observation_end = min(observation_end, deadline_at + HOLDER_PROBE_BUDGET_S)\n', '')
        owner, member = module.Client, 'holder_report'
    elif name == 'late-observation':
        source = changed(source, '    if deadline is not None and _clock() > deadline:\n        return -1, ""                 # no late observation establishes identity\n', '')
        owner, member = module, '_run'
    elif name == 'notice-split':
        source = changed(source, 'max(0.0, deadline - now()) / 2))', 'max(0.0, deadline - now())))')
        owner, member = module, '_deliver'
    else:
        source = changed(source, '''                                                    info.get("proc_start"),
                                                    before_ps=lambda: self._take_holder_probe_token(
                                                        deadline_at=deadline_at))''', '''                                                    info.get("proc_start"))''')
        source = changed(source, '        observation_end = _clock() + HOLDER_PROBE_BUDGET_S',
                         '        self._take_holder_probe_token(deadline_at=deadline_at)\n        observation_end = _clock() + HOLDER_PROBE_BUDGET_S')
        owner, member = module.Client, 'holder_report'
    namespace = dict(module.__dict__)
    exec(compile(source, module.__file__, 'exec'), namespace)
    target = getattr(owner, member)
    mutant = getattr(namespace['Client'], member) if owner is module.Client else namespace[member]
    assert target.__code__.co_freevars == mutant.__code__.co_freevars
    return target, mutant.__code__


class Mutants:
    def __init__(self):
        self.applied, self.failed = {}, set()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item):
        mode = CASES.get(item.originalname)
        if mode is None:
            yield
            return
        target, code = replacement(mode)
        self.applied[item.nodeid] = mode
        # Keep the real module globals: clocks and subprocess stand-ins supplied
        # by the named test still exercise exactly the changed function body.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(target, '__code__', code)
            yield

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' and report.failed:
            self.failed.add(report.nodeid)


if __name__ == '__main__':
    plugin = Mutants()
    paths = ['tests/unit/test_stopped_daemon.py::'+name for name in CASES if not name.startswith('test_terminal')]
    paths.append('tests/unit/test_hooks.py::test_terminal_poll_keeps_delivery_reserve')
    result = pytest.main([*sys.argv[1:], *paths], plugins=[plugin])
    for node, mode in sorted(plugin.applied.items()):
        print(f'{mode}: {node}: '+('KILLED' if node in plugin.failed else 'SURVIVED'))
    assert int(result) == 1, result
    assert len(plugin.failed) == len(plugin.applied) == 7
    assert set(plugin.applied.values()) == set(CASES.values())
    print('All five review mutants killed by seven named test cases; code restored.')
