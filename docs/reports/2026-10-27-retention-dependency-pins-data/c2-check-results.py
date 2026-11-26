"""Recompute continuation 2 hold totals, observed examples and latest coverage.

Run with Python; --write saves c2-summary.json beside the compact hold evidence.
The final guard hold's pre-deselection collection supplies the required plan.
"""
from __future__ import annotations

import collections
import json
from pathlib import Path
import re
import sys

DATA = Path(__file__).resolve().parent
records = []
for path in DATA.glob("c2-*.json"):
    record = json.loads(path.read_text())
    if "hold" in record:
        records.append(record)
records.sort(key=lambda record: record["start"])

latest = {}
holds = []
for record in records:
    phases = collections.defaultdict(dict)
    for result in record["results"]:
        phases[result["node"]][result["phase"]] = result["outcome"]
    outcomes = {}
    for node in record["collected"]:
        seen = phases[node]
        complete = (seen.get("teardown") == "passed" if record.get("all_phases") else
                    record["exitstatus"] in (0, 1))
        outcomes[node] = ("failed" if "failed" in seen.values() else
                          "passed" if complete and seen.get("call") == "passed" else
                          "skipped" if complete and "skipped" in seen.values() else "incomplete")
        latest[node] = {"outcome": outcomes[node], "hold": record["hold"]}
    examples = {}
    for node, stats in record["statistics"].items():
        if isinstance(stats, dict):
            examples[node] = stats
        else:
            match = re.search(r"during generate phase.*?(\d+) passing, (\d+) failing, and (\d+) invalid",
                              stats, re.S)
            if match:
                examples[node] = dict(zip(("observed_passing", "observed_failing", "observed_invalid"),
                                          map(int, match.groups())))
    holds.append({"hold": record["hold"], "start": record["start"], "load": record["load"][0],
                  "seconds": record["seconds"], "exitstatus": record["exitstatus"],
                  "counts": dict(collections.Counter(outcomes.values())), "examples": examples})

plan_file = DATA / "c2-guards-plan.json"
plan = set(json.loads(plan_file.read_text())["all_collected"]) if plan_file.exists() else set()
summary = {"holds": holds, "planned": len(plan),
           "latest_counts": dict(collections.Counter(latest[node]["outcome"] for node in plan & latest.keys())),
           "unrun": sorted(plan - latest.keys()),
           "max_pytest_rss_bytes": max((record["maxrss_bytes"] for record in records), default=0),
           "max_captured_child_rss_bytes": max((record.get("child_maxrss_bytes", 0) for record in records), default=0),
           "unresolved": {node: latest[node] for node in sorted(plan & latest.keys())
                          if latest[node]["outcome"] not in ("passed", "skipped")}}
if "--write" in sys.argv:
    (DATA / "c2-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
