"""routes_at.py <label> <dependencies.py file> <scratch>: run the hub's r10-routes-probe.py unchanged
against a given reader, logging every returned string with both spelling equations and its stat."""
import json
import os
import runpy
import sys
import types

import subfleet
from subfleet import folders

label, source, scratch = sys.argv[1:4]
module = types.ModuleType("subfleet.dependencies")
module.__package__ = "subfleet"
module.__file__ = source
exec(compile(open(source).read(), source, "exec"), module.__dict__)
sys.modules["subfleet.dependencies"] = module
subfleet.dependencies = module

LOG = []
original = module.git_storage


def git_storage(path, *, incomplete=None):
    out = original(path, incomplete=incomplete)
    rows = []
    for place in out:
        eq1 = os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place
        eq2 = folders.canonical(place) == place
        try:
            os.stat(place)
            err = 0
        except OSError as exc:
            err = exc.errno
        rows.append(dict(place=place, bytes=len(os.fsencode(place)), eq1=eq1, eq2=eq2,
                         link=os.path.islink(place), stat=err))
    LOG.append(dict(path=path, returned=rows, incomplete=list(incomplete or [])))
    return out


module.git_storage = git_storage
sys.argv = ["r10-routes-probe.py", scratch]
runpy.run_path("/Users/maxghenis/reviews/subfleet-hub/retention-live-2026-10-27/r10-routes-probe.py", run_name="__main__")
names = ["control", "scan", "alternates-line", "alternates-line-tail", "gitfile", "commondir", "objects+alternates"]
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"routes-{label}.json")
with open(out, "w") as stream:
    json.dump([dict(route=n, **row) for n, row in zip(names, LOG)], stream, indent=1)
    stream.write("\n")
for n, row in zip(names, LOG):
    print(n, "incomplete", [e["file"][-50:] for e in row["incomplete"]])
    for r in row["returned"]:
        print("   ", r["bytes"], "eq1", r["eq1"], "eq2", r["eq2"], "link", r["link"], "stat", r["stat"], "...", r["place"][-70:])
