"""Serial historical model checks; restore exact production bytes in finally.

Run with a fresh, caller-owned TMPDIR. Each run uses the same process world,
production modules from the named commit, and a fixed Hypothesis seed. A known
bug counts only when pytest minimizes an S1/S2 assertion counterexample.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ("887552207", "failed-bracket-child", "S1 premature release"),
    ("951d9623f", "reused-before-group", "S1 premature release"),
    ("951d9623f", "retained-authority", "S2 stray signal"),
    ("684dc4d130c5", "reused-before-group", "S1 premature release"),
    ("684dc4d130c5", "retained-authority", "S2 stray signal"),
)


def main():
    git = ["git", "--git-dir=.git-local"] if (ROOT / ".git-local").exists() else ["git"]
    paths = [ROOT / "subfleet" / name for name in ("procs.py", "daemon.py")]
    originals = {p: p.read_bytes() for p in paths}
    evidence = ROOT / "docs/reports/pr131-fix7"
    evidence.mkdir(exist_ok=True)
    found = 0
    try:
        for revision, scenario, assertion in CASES:
            for p in paths:
                p.write_bytes(subprocess.check_output(
                    [*git, "show", f"{revision}:{p.relative_to(ROOT)}"], cwd=ROOT))
            with tempfile.TemporaryDirectory(prefix="sf-history-cache-", dir=os.environ["TMPDIR"]) as cache:
                result = subprocess.run([sys.executable, "-B", "-X", f"pycache_prefix={cache}",
                    "-m", "pytest", "-xq", "tests/fake/test_quarantine_process_world.py::TestProcessWorld",
                    "--hypothesis-seed=131"], cwd=ROOT, capture_output=True, text=True, timeout=540,
                    env={**os.environ, "SF_WORLD_SCENARIO": scenario, "SF_WORLD_EXAMPLES": "30"})
            (evidence / f"model-{revision}-{scenario}.txt").write_text(result.stdout + result.stderr)
            detected = result.returncode == 1 and assertion in result.stdout and "Falsifying example:" in result.stdout
            found += int(detected)
            print(f"{'FOUND' if detected else 'MISSED'} {revision} {scenario}: {assertion}", flush=True)
    finally:
        for p, original in originals.items():
            p.write_bytes(original)
            assert p.read_bytes() == original
    print(f"{found}/{len(CASES)} historical bugs found; production bytes restored", flush=True)
    return int(found != len(CASES))


if __name__ == "__main__":
    sys.exit(main())
