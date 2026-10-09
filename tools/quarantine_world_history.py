"""Serial historical model checks; restore exact production bytes in finally.

Run with a fresh, caller-owned TMPDIR. Each run uses the same process world,
production modules from the named commit, and a fixed Hypothesis seed. A known
bug counts only when pytest minimizes an S1/S2 assertion counterexample.
"""
from pathlib import Path
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ("887552207", "failed-bracket-child", "S1 premature release", "attempt"),
    ("951d9623f", "reused-before-group", "S1 premature release", "attempt"),
    ("951d9623f", "retained-authority", "S2 stray signal", "attempt"),
    ("951d9623f", "retained-authority", "S2 stray signal", "probe"),
    ("684dc4d130c5", "reused-before-group", "S1 premature release", "attempt"),
    ("684dc4d130c5", "retained-authority", "S2 stray signal", "attempt"),
    ("684dc4d130c5", "retained-authority", "S2 stray signal", "probe"),
)


def main():
    git = ["git", "--git-dir=.git-local"] if (ROOT / ".git-local").exists() else ["git"]
    paths = [ROOT / "subfleet" / name for name in ("procs.py", "daemon.py")]
    originals = {p: p.read_bytes() for p in paths}
    evidence = ROOT / "docs/reports/pr131-fix7"
    evidence.mkdir(exist_ok=True)
    found = 0
    summary = []
    try:
        for revision, scenario, assertion, consumer in CASES:
            for p in paths:
                p.write_bytes(subprocess.check_output(
                    [*git, "show", f"{revision}:{p.relative_to(ROOT)}"], cwd=ROOT))
            # Keep installed-library caches; only the changing production
            # modules need isolation. -B prevents new production caches.
            for p in paths:
                for cache in (p.parent / "__pycache__").glob(p.stem + ".*.pyc"):
                    cache.unlink()
            print(f"CHECK {revision} {scenario} {consumer}", flush=True)
            started = time.monotonic()
            with subprocess.Popen([sys.executable, "-B", "-m", "pytest", "-xq",
                    "tests/fake/test_quarantine_process_world.py::TestProcessWorld",
                    "--hypothesis-seed=131"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                    "SF_WORLD_SCENARIO": scenario, "SF_WORLD_CONSUMER": consumer,
                                    "SF_WORLD_EXAMPLES": "30"}) as child:
                try:
                    stdout, stderr = child.communicate(timeout=540)
                except BaseException:
                    child.terminate()
                    try:
                        child.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.communicate()
                    raise
                result = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
            (evidence / f"model-{revision}-{scenario}-{consumer}.txt").write_text(result.stdout + result.stderr)
            minimized = any(label in result.stdout for label in ("Falsifying example:", "Failing test case:"))
            detected = result.returncode == 1 and assertion in result.stdout and minimized
            found += int(detected)
            outcome = f"{'FOUND' if detected else 'MISSED'} {revision} {scenario} {consumer}: {assertion} ({time.monotonic() - started:.1f}s)"
            summary.append(outcome)
            print(outcome, flush=True)
    finally:
        for p, original in originals.items():
            p.write_bytes(original)
            assert p.read_bytes() == original
    outcome = f"{found}/{len(CASES)} historical bugs found; production bytes restored"
    (evidence / "history-summary.txt").write_text("\n".join([*summary, outcome]) + "\n")
    print(outcome, flush=True)
    return int(found != len(CASES))


if __name__ == "__main__":
    sys.exit(main())
