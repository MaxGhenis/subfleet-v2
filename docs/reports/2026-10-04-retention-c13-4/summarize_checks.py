"""Verify required node coverage, including the two seeded sweep slices."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def main():
    report = Path(__file__).resolve().parent
    required = (report / "required-nodes.txt").read_text().splitlines()
    nodes = []
    files = Counter()
    totals = Counter(tests=0, failures=0, errors=0, skipped=0)
    seconds = []
    names = []
    for index in range(40):
        name = f"final-py312-{index:02}"
        if index == 16:
            name += "-retry"
        if index == 17:
            names.extend([name + "-a", name + "-b"])
        else:
            names.append(name)
    for name in names:
        result = json.loads((report / (name + ".json")).read_text())
        assert result["returncode"] == 0 and not result["timed_out"], result
        assert result["seconds"] < 600, result
        seconds.append(result["seconds"])
        xml = ET.parse(report / (name + ".xml")).getroot()
        for suite in xml.iter("testsuite"):
            for key in totals:
                totals[key] += int(suite.get(key, "0"))
        for case in xml.iter("testcase"):
            filename = case.get("classname").replace(".", "/") + ".py"
            nodes.append(filename + "::" + case.get("name"))
            files[filename] += 1
    expected = Counter(required)
    sweep = "tests/unit/test_retention_sweep_race.py::test_no_schedule_of_sweep_moves_loses_or_orphans_a_tree"
    expected[sweep] += 1  # Two seeded 20-example slices cover its 40 generated examples.
    assert Counter(nodes) == expected, {
        "missing": sorted((expected - Counter(nodes)).elements()),
        "extra": sorted((Counter(nodes) - expected).elements()),
    }
    summary = dict(
        code_head=(report / "final-code-head.txt").read_text().strip(),
        python="3.12.14",
        counts=dict(totals), files=dict(sorted(files.items())),
        foreground_slices=len(names), unique_nodes=len(set(nodes)),
        failed_validation_attempts=2, unknown_runner_exits=1, timeout_attempts=1,
        sweep_generated_examples=40, sweep_seeds=[2170001, 2170002],
        max_slice_seconds=max(seconds), summed_slice_seconds=round(sum(seconds), 2),
        complete_required_coverage=True,
    )
    (report / "final-py312-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
