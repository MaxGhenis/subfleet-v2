"""mutant.py <name>...: replay fix r12's named mutants in-process against their named killers.

Each mutant's edits (fix r12's mutants.json, unchanged) are applied by exec into the imported
module's namespace, the killers run with --tb=short, and the failing assertion lines are compared
with fix r12's receipt for the same mutant. One pytest.main per mutant, each in a fresh process."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time

import pytest

HERE = Path(__file__).resolve().parent
FIX = HERE.parent / "2026-10-31-retention-pins-fix-r12-data"


def assertions(text):
    return [line.strip() for line in text.splitlines() if line.startswith("    assert ")]


class Calls:
    def __init__(self):
        self.rows = []

    def pytest_runtest_logreport(self, report):
        if report.when == "call" or report.outcome == "failed":
            self.rows.append(dict(node=report.nodeid, phase=report.when, outcome=report.outcome,
                                  failure=str(report.longrepr) if report.failed else ""))


name = sys.argv[1]
definition = json.loads((FIX / "mutants.json").read_text())[name]
sources, hashes = {}, []
for edit in definition["edits"]:
    module = importlib.import_module(edit["module"])
    source = sources.get(edit["module"], Path(module.__file__).read_text())
    assert source.count(edit["old"]) == 1, (name, source.count(edit["old"]))
    changed = source.replace(edit["old"], edit["new"])
    sources[edit["module"]] = changed
    exec(compile(changed, module.__file__, "exec"), module.__dict__)
    hashes.append(dict(original=hashlib.sha256(source.encode()).hexdigest(),
                       mutated=hashlib.sha256(changed.encode()).hexdigest()))
calls = Calls()
start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
load = os.getloadavg()
rc = pytest.main(["-q", "-p", "no:cacheprovider", "--tb=short", *definition["tests"]], plugins=[calls])
receipt = json.loads((FIX / f"mutant-{name}.json").read_text())
prior = [p for p in receipt["phases"] if p["phase"] == "call" and p["outcome"] == "failed"]
mine = [r for r in calls.rows if r["phase"] == "call" and r["outcome"] == "failed"]
other = [r for r in calls.rows if r["phase"] != "call" and r["outcome"] == "failed"]
row = dict(name=name, start=start, load=load, rc=int(rc), killers=definition["tests"], hashes=hashes,
           failed=len(mine), other_failures=len(other), killed=int(rc) == 1 and len(mine) == len(definition["tests"]) and not other,
           assertions=[assertions(r["failure"]) for r in mine],
           fix_r12_assertions=[assertions(p["failure"]) for p in prior],
           same_as_fix_r12=[assertions(r["failure"]) for r in mine] == [assertions(p["failure"]) for p in prior])
out = HERE / "mutants"
out.mkdir(exist_ok=True)
(out / f"{name}.json").write_text(json.dumps(dict(row, failures=[r["failure"][-3000:] for r in mine]), indent=1) + "\n")
print("MUTANT", json.dumps(dict((k, row[k]) for k in ("name", "rc", "failed", "killed", "same_as_fix_r12", "assertions"))))
