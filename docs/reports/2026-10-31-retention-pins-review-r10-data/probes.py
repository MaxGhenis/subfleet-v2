"""Pre-repair executions: real Git fixtures, no live stores or processes."""
import json
from pathlib import Path
import types

import pytest

from subfleet import dependencies, folders
from tests.fake.test_retention_dependency_review_r9 import data_alias
from tests.unit.test_retention_locations import routine_input


def version(source, label):
    module = types.ModuleType('subfleet.r9_' + label)
    exec(compile(source, '<' + label + '>', 'exec'), module.__dict__)
    return module


@pytest.mark.parametrize('layout', ['plain', 'worktree'])
def test_start_before_repair(tmp_path, layout, record_property):
    checkout, expected = routine_input(tmp_path, layout)
    alias = str(data_alias(checkout))
    source = Path(__file__).with_name('base-dependencies.py.txt').read_text()
    trial = source.replace('self.files.add(resolved)', 'self.files.add(folders.canonical(resolved))')
    trial = trial.replace('aliases.files.add(resolved)', 'aliases.files.add(folders.canonical(resolved))')
    trial = trial.replace('self.entries.add(self.resolve(entry.path))',
                          'self.entries.add(folders.canonical(self.resolve(entry.path)))')
    fixed = trial.replace('ancestors = {start, *folders.above(start)}',
                          'start = folders.canonical(start)\n        ancestors = {start, *folders.above(start)}')
    observations = {}
    for label, text in [('base', source), ('three-sites', trial), ('with-start', fixed)]:
        module = version(text, label)
        errors = []
        storage = module.git_storage(alias, incomplete=errors)
        recorded = module.record(alias)
        assert not errors and not recorded.get('incomplete')
        observations[label] = dict(git_storage=storage, record=recorded)
    assert observations['base']['record'] == expected
    assert observations['with-start']['record'] == expected
    assert (observations['three-sites']['record'] != expected) == (layout == 'worktree')
    record_property('start-probe', json.dumps(dict(layout=layout, input=alias,
        canonical=folders.canonical(checkout), expected=expected, observations=observations), sort_keys=True))


@pytest.mark.parametrize('layout', ['plain', 'worktree', 'shared', 'new-workdir', 'none'])
def test_routine_against_236c80883(tmp_path, layout, record_property):
    checkout, expected = routine_input(tmp_path, layout)
    baseline = version(Path(__file__).with_name('base-dependencies.py.txt').read_text(), '236c80883')
    before = baseline.record(str(checkout))
    after = dependencies.record(str(checkout))
    assert before == after == expected
    record_property('routine-comparison', json.dumps(dict(layout=layout, base=before,
                                                         current=after, expected=expected), sort_keys=True))
