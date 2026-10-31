"""Show the moved check's exception semantics without any successful bad record."""
import inspect,json,os
import pytest
from subfleet import dependencies,folders
from tests.unit.test_retention_location_inputs import repo
from round_probes import BASE

@pytest.mark.parametrize('version',['baseline','head'])
def test_spelling_exception_and_check_order(tmp_path,monkeypatch,version,record_property):
 checkout=repo(tmp_path.resolve()/'checkout');module=BASE if version=='baseline' else dependencies
 original=module._Aliases.check;canonical=folders.canonical;calls=[]
 def checking(self,incomplete):calls.append(True);return original(self,incomplete)
 def spelling(path):
  caller=inspect.currentframe().f_back.f_code.co_name
  if str(path)==str(checkout/'.git') and caller=='<genexpr>':raise OSError('controlled final spelling failure')
  return canonical(path)
 with monkeypatch.context() as patch:
  patch.setattr(module._Aliases,'check',checking);patch.setattr(folders,'canonical',spelling)
  with pytest.raises(OSError,match='controlled final spelling failure'):module.record(str(checkout))
 assert len(calls)==(1 if version=='baseline' else 0)
 record_property('exception-check-order',json.dumps(dict(version=version,check_calls=len(calls),returned_record=False)))
