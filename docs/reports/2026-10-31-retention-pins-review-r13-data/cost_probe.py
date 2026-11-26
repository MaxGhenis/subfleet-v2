"""cost_probe.py <scratch>: one routine record at head vs ced02912b, alternating, with os.stat counts."""
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import types

from subfleet import dependencies
from tests.unit.test_retention_locations import routine_input

HERE = Path(__file__).resolve().parent
ced = types.ModuleType("subfleet.r13_ced")
ced.__package__ = "subfleet"
exec(compile((HERE.parent / "2026-10-31-retention-pins-fix-r11-data" / "ced-dependencies.py.txt").read_text(),
             "ced02912b:subfleet/dependencies.py", "exec"), ced.__dict__)

result = {}
for layout in ["plain", "worktree"]:
    with tempfile.TemporaryDirectory(dir=sys.argv[1]) as directory:
        checkout, expected = routine_input(Path(directory), layout)
        assert dependencies.record(str(checkout)) == ced.record(str(checkout)) == expected
        counts = {}
        real_stat = os.stat
        for name, module in (("ced02912b", ced), ("head", dependencies)):
            calls = []
            def counted(*a, **k):
                calls.append(a[0] if a else None)
                return real_stat(*a, **k)
            os.stat = counted
            try:
                module.record(str(checkout))
            finally:
                os.stat = real_stat
            counts[name] = len(calls)
        times = {"ced02912b": [], "head": []}
        for _ in range(40):
            for name, module in (("ced02912b", ced), ("head", dependencies)):
                start = time.perf_counter_ns()
                module.record(str(checkout))
                times[name].append((time.perf_counter_ns() - start) / 1e6)
        result[layout] = dict(stat_calls=counts, load=os.getloadavg(),
                              median_ms={k: round(statistics.median(v), 3) for k, v in times.items()},
                              min_ms={k: round(min(v), 3) for k, v in times.items()})
print(json.dumps(result, indent=1))
(HERE / "cost.json").write_text(json.dumps(result, indent=1) + "\n")
