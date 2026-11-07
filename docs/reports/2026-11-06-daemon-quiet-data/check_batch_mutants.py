"""Run the batch-query mutants against temporary package copies only.

Run with the repository's development Python; no original source is mutated.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def main():
    source = (REPO / "subfleet/store.py").read_text()
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "batch_submission_rows")
    lines = source.splitlines(keepends=True)
    old_walk = '''def batch_submission_rows(query, job_ids):
    if not job_ids:
        return []
    marks = ",".join("?" for _ in job_ids)
    return list(query(f"SELECT job_id,data_json FROM events WHERE +kind='job.submitted' "
                      f"AND job_id IN ({marks}) AND data_json LIKE '%\\"batch\\"%'", job_ids))
'''
    mutations = (
        ("walk-every-event", "".join(lines[:function.lineno - 1]) + old_walk +
         "".join(lines[function.end_lineno:]), "test_later_events_do_not_increase_batch_lookup_steps"),
        ("last-submission", source.replace("ORDER BY first.event_id LIMIT 1",
                                           "ORDER BY first.event_id DESC LIMIT 1"),
         "test_first_submission_defines_batch_even_if_a_malformed_store_repeats_it"),
    )
    results = []
    scratch = Path("/private/tmp/daemon-quiet-build")
    scratch.mkdir(exist_ok=True)
    for name, mutated, test in mutations:
        assert mutated != source
        with tempfile.TemporaryDirectory(prefix=f"batch-mutant-{name}-", dir=scratch) as directory:
            root = Path(directory)
            shutil.copytree(REPO / "subfleet", root / "subfleet",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (root / "subfleet/store.py").write_text(mutated)
            target = root / "test_batch_submission_queries.py"
            shutil.copy2(REPO / "tests/unit/test_batch_submission_queries.py", target)
            command = [sys.executable, "-m", "pytest", str(target), "-k", test,
                       "--basetemp", str(root / "pytest-temp"), "-q"]
            env = {**os.environ, "PYTHONPATH": str(root)}
            result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
            output = result.stdout + result.stderr
            (HERE / f"batch-mutant-{name}.txt").write_text(output)
            killed = result.returncode == 1 and "AssertionError" in output
            results.append({"mutant": name, "test": test, "returncode": result.returncode,
                            "killed": killed, "summary": output.splitlines()[-1] if output else ""})
    (HERE / "batch-mutants.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    assert all(result["killed"] for result in results), results


if __name__ == "__main__":
    main()
