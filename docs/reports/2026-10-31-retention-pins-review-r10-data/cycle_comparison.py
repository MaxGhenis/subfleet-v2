"""Compare the invariant counterexample with the round base and a local repair trial."""
import json
from pathlib import Path
import pytest
from tests.unit.test_retention_location_inputs import repo,git
from subfleet import folders, dependencies
from probes import version

@pytest.mark.parametrize('cycle',['self-suffix','two-link-suffix'])
def test_cycle_comparison(tmp_path,cycle,record_property):
    base=tmp_path.resolve();checkout=repo(base/'checkout');loop=base/'loop'
    if cycle=='self-suffix':loop.symlink_to('loop/tail')
    else:
        loop.symlink_to('other/tail');(base/'other').symlink_to('loop/extra')
    (checkout/'.git'/'unused').symlink_to(loop)
    original=Path(__file__).with_name('base-dependencies.py.txt').read_text()
    modules={'base':version(original,'cycle_base'),'head':dependencies}
    rows={}
    for label,module in modules.items():
        data=module.record(str(checkout))
        assert not data.get('incomplete')
        differences=[p for p in data['git'] if folders.canonical(p)!=p or str(Path(folders.canonical(str(Path(p).parent)))/Path(p).name)!=p]
        assert len(differences)==1, data
        rows[label]=dict(recorded=data,differences=differences)
    assert git('-C',checkout,'show','HEAD:f.txt').returncode==0
    record_property('cycle-comparison',json.dumps(dict(cycle=cycle,rows=rows)))
