"""Exercise cycle receipts with link-reached and CI-sized path prefixes."""
import errno, json, os
from pathlib import Path
import pytest
from subfleet import dependencies,folders
from tests.unit.test_retention_location_cycles import ROWS, cycle_route, expected_error_files
from round_probes import BASE,discover

@pytest.mark.parametrize('target_length',[180,260])
@pytest.mark.parametrize('route,cycle,git_exit,error_count',ROWS)
def test_cycle_inputs_under_longer_linked_root(tmp_path,target_length,route,cycle,git_exit,error_count,record_property):
 # A physical fixture base of exactly this many bytes, longer than the ordinary
 # /private/var/folders/.../T/pytest-of-runner/... fixture paths in CI.
 base=tmp_path.resolve()
 while len(os.fsencode(base))<target_length:
  remaining=target_length-len(os.fsencode(base))-1
  if remaining<=0:break
  base=base/('d'*min(remaining,100));base.mkdir()
 link=tmp_path.resolve()/'via-link';link.symlink_to(base,target_is_directory=True)
 checkout=cycle_route(link,route,cycle)
 before,old=discover(BASE,checkout);after,new=discover(dependencies,checkout)
 assert before.get('incomplete',[])==after.get('incomplete',[])
 assert len(after.get('incomplete',[]))==error_count
 assert [e['file'] for e in after.get('incomplete',[])]==expected_error_files(base,route,cycle)
 assert all(e['parent'] and e['canonical'] for e in new)
 path_max=os.pathconf(base,'PC_PATH_MAX')
 files=[len(os.fsencode(e['file'])) for e in after.get('incomplete',[])]
 row=dict(route=route,cycle=cycle,base_length=len(os.fsencode(base)),path_max=path_max,error_file_lengths=files)
 assert max(files,default=0)<path_max
 record_property('ci-path',json.dumps(row))
