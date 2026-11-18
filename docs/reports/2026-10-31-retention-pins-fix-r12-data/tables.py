"""Join completed receipts; never infer a pass from a started invocation."""
from collections import Counter, defaultdict
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]


def receipts():
    values = []
    for path in DATA.glob("*.json"):
        value = json.loads(path.read_text())
        if isinstance(value, dict) and "phases" in value:
            values.append(value)
    return sorted(values, key=lambda r: r["start"])


def assertions(failure):
    return [line.strip() for line in failure.splitlines() if line.startswith("    assert ")]


def build():
    runs = receipts()
    complete = [r for r in runs if "exit" in r]
    ordinary = [r for r in complete if not r["name"].startswith(("red-", "mutant-"))]
    passed = {p["node"] for r in ordinary for p in r["phases"]
              if p["phase"] == "call" and p["outcome"] == "passed"}
    collected = {node for r in ordinary for node in r.get("collected", [])}
    by_file = defaultdict(set)
    for node in passed:
        by_file[node.split("::")[0]].add(node)
    missing = sorted(collected - passed)
    names = {r["name"] for r in complete}
    for split_path in DATA.glob("*-split.json"):
        split = json.loads(split_path.read_text())
        if split.get("split_into") and all(child["name"] in names and child["exit"] == 0 for child in split["split_into"]):
            names.add(split["name"])
    plan = json.loads((DATA / "required-plan.json").read_text())
    not_run = [n for n, _ in plan if n not in names]
    comparisons = []
    for name in ("green-readable", "linked-readable"):
        run = next((r for r in complete if r["name"] == name), None)
        if run:
            comparisons.extend(dict(hold=name, **json.loads(o["value"]))
                               for o in run["observations"] if o["key"] == "differential")
    strategy_counts = {}
    for path in DATA.glob("differential-*.json"):
        if path.name == "differential-summary.json":
            continue
        observations = json.loads(path.read_text())
        strategy_counts[path.stem] = len(observations)
        comparisons.extend(dict(hold=path.stem, **r) for r in observations)
    definitions = json.loads((DATA / "mutants.json").read_text())
    mutants = []
    for name, definition in definitions.items():
        run = next((r for r in complete if r["name"] == "mutant-" + name), None)
        if run is None:
            mutants.append(dict(name=name, result="unrun", killer=definition["tests"][0]))
            continue
        failures = [p for p in run["phases"] if p["phase"] == "call" and p["outcome"] == "failed"]
        other_failures = [p for p in run["phases"] if p["phase"] != "call" and p["outcome"] == "failed"]
        killed = run["exit"] == 1 and len(failures) == len(definition["tests"]) and not other_failures
        row = dict(name=name, killer="; ".join(definition["tests"]), killers=definition["tests"], failed_assertions=len(failures), result="killed" if killed else "not killed",
                   assertions=assertions(failures[0]["failure"]) if failures else [],
                   start=run["start"], load=run["load"])
        for label, key in (("r11", "previous_r11_result"), ("r10", "previous_r10_result"), ("r9", "previous_r9_result"),
                           ("r8", "previous_result")):
            if key in definition:
                old = json.loads((ROOT / definition[key]).read_text())
                prior = [p for p in old["phases"] if p["phase"] == "call" and p["outcome"] == "failed"]
                row["same_" + label + "_assertion"] = (len(prior) == 1 and bool(row["assertions"])
                    and row["assertions"] == assertions(prior[0]["failure"]))
        mutants.append(row)
    summary = dict(completed=len(complete), ordinary_invocations=len(ordinary),
        unique_passed=len(passed), per_file={f: len(v) for f, v in sorted(by_file.items())},
        missing_or_unfinished=missing, planned_holds_not_completed=not_run,
        active=[r["name"] for r in runs if "exit" not in r],
        locked=all(r["locked"] for r in complete),
        longest_seconds=max((r["seconds"] for r in complete), default=0),
        peak_rss_mib=max((r["peak_rss_mib"] for r in complete), default=0),
        differential_comparisons=len(comparisons), new_module_comparisons=len(comparisons)-sum(strategy_counts.values()),
        unchanged_strategy_comparisons=strategy_counts, mutants=mutants)
    summary['ordinary_failures'] = [dict(hold=r['name'], node=p['node'], phase=p['phase'],
        later_passed=p['node'] in passed) for r in ordinary for p in r['phases'] if p['outcome'] == 'failed']
    summary['interrupted'] = [dict(hold=r['name'], seconds=r['seconds'],
        remaining=[n for n in r.get('collected', []) if n not in passed])
        for r in ordinary if r['exit'] == 2]
    (DATA / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (DATA / "mutant-results.json").write_text(json.dumps(mutants, indent=2) + "\n")
    (DATA / "differential-summary.json").write_text(json.dumps(dict(
        comparisons=len(comparisons), dropped=Counter(str(v["errno"]) for r in comparisons for v in r["dropped"]),
        evidence_matches=all(r["actual"].get("incomplete", []) == r["baseline"].get("incomplete", []) ==
                             r["r10"].get("incomplete", []) for r in comparisons)), indent=2) + "\n")
    lines = ["| Hold / file selection | Pytest start UTC | Load 1/5/15 | Passed / failed / skipped | Seconds | Exit |",
             "|---|---|---|---:|---:|---:|"]
    for r in complete:
        counts = Counter(p["outcome"] for p in r["phases"] if p["phase"] == "call")
        # setup skips have no call report.
        counts["skipped"] += sum(p["phase"] == "setup" and p["outcome"] == "skipped" for p in r["phases"])
        load = "/".join(f"{v:.2f}" for v in r["load"])
        duration = ("≤" if r.get("seconds_is_upper_bound") else "") + f'{r["seconds"]:.2f}'
        lines.append(f'| [{r["name"]}]({r["name"]}.json) | {r["start"]} | {load} | '
                     f'{counts["passed"]}/{counts["failed"]}/{counts["skipped"]} | {duration} | {r["exit"]} |')
    (DATA / "hold-table.md").write_text("\n".join(lines) + "\n")
    per_file = ["| File | Unique passed nodes |", "|---|---:|"]
    per_file.extend(f"| {f} | {len(v)} |" for f, v in sorted(by_file.items()))
    (DATA / "file-table.md").write_text("\n".join(per_file) + "\n")
    mutant_lines = ["| Mutant | Named killer | Result | Same assertion at r11 / r10 / r9 / r8 |", "|---|---|---|---|"]
    for row in mutants:
        same = "/".join("yes" if row["same_" + label + "_assertion"] else "NO"
                        for label in ("r11", "r10", "r9", "r8") if "same_" + label + "_assertion" in row)
        mutant_lines.append(f'| {row["name"]} | {row["killer"]} | {row["result"]} | {same or "new"} |')
    (DATA / "mutant-table.md").write_text("\n".join(mutant_lines) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("mutants", "per_file")}, indent=2))
    return summary


if __name__ == "__main__":
    build()
