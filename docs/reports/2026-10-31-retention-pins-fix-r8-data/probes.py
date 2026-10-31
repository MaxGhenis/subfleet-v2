"""Executed evidence, including the deliberately bounded deeper-link difference."""
import json
from pathlib import Path
import shutil
import types

import pytest

from subfleet import dependencies, folders
from tests.unit.test_retention_dependency_units import repo
from tests.unit.test_retention_locations import routine_input
from tests.unit.test_retention_pointer_whitespace import git


@pytest.mark.parametrize('layout', ['plain', 'worktree', 'shared', 'new-workdir', 'none'])
def test_routine_matches_prerequisite(tmp_path, layout, record_property):
    module = types.ModuleType('subfleet.r8_base_dependencies')
    source = Path(__file__).with_name('base-dependencies.py.txt').read_text()
    exec(compile(source, '<373a7f5bf:dependencies>', 'exec'), module.__dict__)
    checkout, expected = routine_input(tmp_path, layout)
    before = module.record(str(checkout))
    after = dependencies.record(str(checkout))
    assert before == after == expected
    record_property('base-comparison', json.dumps(dict(layout=layout, base=before, fix=after), sort_keys=True))


def test_deeper_ref_link_is_the_documented_difference(tmp_path, record_property):
    base = tmp_path.resolve()
    owner = base / 'owner'
    owner.mkdir()
    stable = repo(base / 'stable')
    checkout = base / 'checkout'
    checkout.mkdir()
    (checkout / '.git').write_text('gitdir: ' + str(stable / '.git') + '\n')
    branch = stable / '.git' / 'refs' / 'heads' / 'main'
    branch.rename(owner / 'branch')
    branch.symlink_to(owner / 'branch')
    before = git('-C', checkout, 'show', 'HEAD:f.txt')
    assert before.returncode == 0 and before.stdout == b'stable', before.stderr
    recorded = dependencies.record(str(checkout))
    assert not recorded.get('incomplete'), recorded
    assert not any(folders.within(place, str(owner)) for place in recorded['git']), recorded
    shutil.rmtree(owner)
    after = git('-C', checkout, 'show', 'HEAD:f.txt')
    assert after.returncode != 0
    record_property('D-R8-1', json.dumps(dict(input='refs/heads/main -> owner/branch', recorded=recorded,
                                             before=before.returncode, after=after.returncode,
                                             error=after.stderr.decode()), sort_keys=True))
