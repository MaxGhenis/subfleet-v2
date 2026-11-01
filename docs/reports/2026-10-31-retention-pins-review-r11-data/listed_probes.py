import json,shutil
from pathlib import Path
from subfleet import dependencies,folders
from tests.unit.test_retention_dependency_units import repo
from tests.unit.test_retention_pointer_whitespace import git

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


def test_unused_top_entry_is_a_converse_bound_counterexample(tmp_path, record_property):
    base = tmp_path.resolve()
    stable = repo(base / 'stable')
    unrelated = base / 'unrelated'
    unrelated.mkdir()
    checkout = base / 'checkout'
    checkout.mkdir()
    (checkout / '.git').write_text('gitdir: ' + str(stable / '.git') + '\n')
    (unrelated / 'unused').write_text('not Git metadata')
    (stable / '.git' / 'unused-entry').symlink_to(unrelated / 'unused')
    before = git('-C', checkout, 'show', 'HEAD:f.txt')
    recorded = dependencies.record(str(checkout))
    assert before.returncode == 0 and not recorded.get('incomplete'), recorded
    assert str(unrelated / 'unused') in recorded['git'], recorded
    shutil.rmtree(unrelated)
    after = git('-C', checkout, 'show', 'HEAD:f.txt')
    assert after.returncode == 0 and after.stdout == before.stdout
    record_property('converse-bound-counterexample', json.dumps(dict(
        input='stable/.git/unused-entry -> unrelated/unused', recorded=recorded,
        before=before.returncode, after=after.returncode), sort_keys=True))
