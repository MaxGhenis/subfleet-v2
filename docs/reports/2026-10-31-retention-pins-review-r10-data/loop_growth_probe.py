"""C-8.4: observe stable unused scan links with a suffix-growing cycle."""
import json
from pathlib import Path
import pytest
from subfleet import dependencies, folders
from tests.unit.test_retention_location_inputs import repo,git

@pytest.mark.parametrize('kind',['self-suffix','two-link-suffix','dangling-suffix'])
def test_suffix_cycle_spelling(tmp_path,kind,record_property):
    base=tmp_path.resolve();checkout=repo(base/'checkout');loop=base/'loop'
    if kind=='self-suffix':loop.symlink_to('loop/tail')
    elif kind=='two-link-suffix':
        other=base/'other';loop.symlink_to('other/tail');other.symlink_to('loop/extra')
    else:loop.symlink_to('absent/tail')
    (checkout/'.git'/'unused').symlink_to(loop)
    read=git('-C',checkout,'show','HEAD:f.txt');assert read.returncode==0,read.stderr
    recorded=dependencies.record(str(checkout));assert not recorded.get('incomplete'),recorded
    differences=[dict(place=p,canonical=folders.canonical(p),parent=folders.canonical(str(Path(p).parent))) for p in recorded.get('git',[]) if folders.canonical(p)!=p or str(Path(folders.canonical(str(Path(p).parent)))/Path(p).name)!=p]
    record_property('suffix-cycle',json.dumps(dict(kind=kind,recorded=recorded,differences=differences)))
