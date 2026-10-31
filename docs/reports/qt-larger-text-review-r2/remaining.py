"""Sequential requested holds; stays attached to the tool session and waits for each child."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DEADLINE = datetime.datetime.fromisoformat("2026-10-31T09:19:50+00:00")
MUTATIONS = "tests/frontend/test_review_r219_fix8_mutations.py"
runs = [
    ("repeat-larger", json.loads((HERE / "repeat.args.json").read_text()), {}),
    ("fix8-view", ["tests/frontend/test_review_r219_fix8_view.py"], {}),
    ("resizes", ["tests/frontend/test_review_pr284_resizes.py", "-s"], {}),
    ("round7-marker", [MUTATIONS, "-k", "round7 and marker_releases_pending_reveal", "-s"], {}),
    ("round7-distance", [MUTATIONS, "-k", "round7 and confirm_by_distance_only", "-s"], {}),
    ("round7-never-lands", [MUTATIONS, "-k", "round7 and reveal_never_lands", "-s"], {}),
    ("round7-larger", [MUTATIONS, "-k", "round7", "-s"], {"R219_PROBE_TEXT_SCALE": "1.728"}),
    ("round10-quiet", [MUTATIONS, "-k", "test_round10_mutation_fails_its_named_regression and quiet_landing_ends_following", "-s"], {}),
    ("round10-growth", [MUTATIONS, "-k", "growth_repins_dropped or test_scripted_regrow_then_shrink_reaches_the_growth_mutant_check", "-s"], {}),
    ("round10-window", [MUTATIONS, "-k", "test_round10_mutation_fails_its_named_regression and quiet_window_0ms", "-s"], {}),
    ("diagnostics", ["-p", "tests.conftest", "docs/reports/qt-larger-text-review-r2/diagnostics.py", "-s"], {}),
    ("quiet-forced", ["tests/frontend/test_review_pr284_pin.py::test_quiet_landing_rule_rejects_a_measured_missed_growth", "--runxfail", "-s"], {}),
]
results = []
for index, (name, args, extra) in enumerate(runs):
    if datetime.datetime.now(datetime.timezone.utc) >= DEADLINE:
        (HERE / "unrun.json").write_text(json.dumps(runs[index:], indent=2) + "\n")
        break
    env = os.environ.copy()
    env.update(extra)
    child = subprocess.Popen([sys.executable, str(HERE / "runner.py"), "run", name, str(ROOT), *args],
                             cwd=ROOT, env=env)
    try:
        code = child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait()
    results.append({"name": name, "returncode": code, "extra_environment": extra})
    (HERE / "sequence-results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(name, "finished with", code, flush=True)
