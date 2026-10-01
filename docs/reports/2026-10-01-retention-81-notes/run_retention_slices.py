"""Run the retention suites in foreground slices; no daemon state is used."""
import json
import subprocess
import sys
from pathlib import Path

here = Path("docs/reports/2026-10-01-retention-81-notes")
slices = json.loads((here / "retention-slices.json").read_text())
results = []
for i, nodes in enumerate(slices, 1):
    print(f"Retention foreground slice {i}/{len(slices)} ({len(nodes)} cases)", flush=True)
    command = [sys.executable, str(here / "run_check.py"), str(here / f"retention-slice-{i:02d}.txt"), "550", "uv", "run", "pytest", "-q", *nodes]
    result = subprocess.run(command)
    results.append({"slice": i, "exit": result.returncode, "tests": nodes})
    (here / "retention-results.json").write_text(json.dumps(results, indent=2) + "\n")
    if result.returncode:
        raise SystemExit(result.returncode)
print(f"All {len(slices)} retention slices passed ({sum(len(s) for s in slices)} cases).", flush=True)
