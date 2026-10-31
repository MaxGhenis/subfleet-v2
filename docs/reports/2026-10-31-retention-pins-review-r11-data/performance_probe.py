"""Small alternating routine record sample, using wall and process CPU time."""
import json,statistics,time
from subfleet import dependencies,folders
from tests.unit.test_retention_locations import routine_input
from round_probes import BASE

def test_routine_record_cost(tmp_path,record_property):
 checkout,expected=routine_input(tmp_path,'plain')
 assert BASE.record(str(checkout))==dependencies.record(str(checkout))==expected
 values={k:[] for k in ['base','head']}
 for n in range(30):
  order=[('base',BASE),('head',dependencies)]
  if n%2:order.reverse()
  for label,module in order:
   cpu=time.process_time();wall=time.perf_counter();module.record(str(checkout))
   values[label].append(dict(wall_ms=(time.perf_counter()-wall)*1000,cpu_ms=(time.process_time()-cpu)*1000))
 summaries={label:{field:dict(median=statistics.median(v[field] for v in rows),minimum=min(v[field] for v in rows),maximum=max(v[field] for v in rows)) for field in ['wall_ms','cpu_ms']} for label,rows in values.items()}
 record_property('routine-cost',json.dumps(dict(values=values,summaries=summaries),sort_keys=True))
