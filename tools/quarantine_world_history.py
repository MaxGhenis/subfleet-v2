"""Serial historical model checks; restore exact production bytes in finally.

Run with a fresh, caller-owned TMPDIR. Each run uses the same process world,
production modules from the named commit, and a fixed Hypothesis seed. A known
bug counts only when pytest minimizes an S1/S2/K1 assertion counterexample.
Identity-only S1 replay is a passing historical control: the CI property
failure came from attributing another attempt's census to the current attempt.
Every child has a nine-minute bound within a 25-minute total budget.
CLI arguments select a revision, scenario, or REVISION:SCENARIO:CONSUMER.
"""
from pathlib import Path
import os
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ("977999487", "paced-missing-start", "S1 premature release", "attempt"),
    ("887552207", "failed-bracket-child", "S1 premature release", "attempt"),
    ("951d9623f", "reused-before-group", "S1 premature release", "attempt"),
    ("951d9623f", "retained-authority", "S2 stray signal", "attempt"),
    ("951d9623f", "retained-authority", "S2 stray signal", "probe"),
    ("684dc4d130c5", "reused-before-group", "S1 premature release", "attempt"),
    ("684dc4d130c5", "retained-authority", "S2 stray signal", "attempt"),
    ("684dc4d130c5", "retained-authority", "S2 stray signal", "probe"),
    ("169f6b6c1", "owned-without-shape", "K1 owned survivor not signalled", "attempt"),
    ("a026a52f5", "owned-without-shape", "K1 owned survivor not signalled", "attempt"),
    ("169f6b6c1", "owned-without-shape", "K1 owned survivor not signalled", "probe"),
    ("a026a52f5", "owned-without-shape", "K1 owned survivor not signalled", "probe"),
    ("169f6b6c1", "owned-after-census-escape", "K1 owned survivor not signalled", "attempt"),
    ("a026a52f5", "owned-after-census-escape", "K1 owned survivor not signalled", "attempt"),
    ("169f6b6c1", "owned-after-census-escape", "K1 owned survivor not signalled", "probe"),
    ("a026a52f5", "owned-after-census-escape", "K1 owned survivor not signalled", "probe"),
    ("169f6b6c1", "paced-owned-table-outage", "K1 owned survivor not signalled", "attempt"),
    ("a026a52f5", "paced-owned-table-outage", "K1 owned survivor not signalled", "attempt"),
    ("169f6b6c1", "paced-owned-table-outage", "K1 owned survivor not signalled", "probe"),
    ("a026a52f5", "paced-owned-table-outage", "K1 owned survivor not signalled", "probe"),
)
QUIET_CASES = (
    ("169f6b6c1", "recorded-identity-restart", "S1 premature release", "attempt"),
    ("a026a52f5", "recorded-identity-restart", "S1 premature release", "attempt"),
    ("current", "recorded-identity-restart", "S1 premature release", "attempt"),
    ("current", "owned-without-shape", "K1 owned survivor not signalled", "attempt"),
    ("current", "owned-without-shape", "K1 owned survivor not signalled", "probe"),
    ("current", "owned-after-census-escape", "K1 owned survivor not signalled", "attempt"),
    ("current", "owned-after-census-escape", "K1 owned survivor not signalled", "probe"),
    ("current", "paced-owned-table-outage", "K1 owned survivor not signalled", "attempt"),
    ("current", "paced-owned-table-outage", "K1 owned survivor not signalled", "probe"),
)


def _run_history():
    requested = set(sys.argv[1:])
    def selected(case):
        revision, scenario, _, consumer = case
        return not requested or bool(requested & {revision, scenario, f"{revision}:{scenario}:{consumer}"})
    cases = tuple(case for case in CASES if selected(case))
    controls = tuple(case for case in QUIET_CASES if selected(case))
    assert cases or controls, "no history checks selected"
    git = ["git", "--git-dir=.git-local"] if (ROOT / ".git-local").exists() else ["git"]
    paths = [ROOT / "subfleet" / name for name in ("procs.py", "daemon.py")]
    originals = {p: p.read_bytes() for p in paths}
    evidence = ROOT / os.environ.get("SF_WORLD_EVIDENCE", "docs/reports/pr131-fix9/history")
    evidence.mkdir(parents=True, exist_ok=True)
    found = 0
    quiet = 0
    summary = []
    deadline = time.monotonic() + 1500
    try:
        checks = [(case, True) for case in cases] + [(case, False) for case in controls]
        for (revision, scenario, assertion, consumer), expect_failure in checks:
            if time.monotonic() >= deadline:
                summary.append("TIME LIMIT: remaining history checks skipped at 25-minute bound")
                print(summary[-1], flush=True)
                break
            for p in paths:
                p.write_bytes(originals[p] if revision == "current" else subprocess.check_output(
                    [*git, "show", f"{revision}:{p.relative_to(ROOT)}"], cwd=ROOT))
            # Keep installed-library caches; only the changing production
            # modules need isolation. -B prevents new production caches.
            for p in paths:
                for cache in (p.parent / "__pycache__").glob(p.stem + ".*.pyc"):
                    cache.unlink()
            for cache in (ROOT / "tests/fake/__pycache__").glob("test_quarantine_process_world.*.pyc"):
                cache.unlink()
            print(f"CHECK {revision} {scenario} {consumer}", flush=True)
            started = time.monotonic()
            with subprocess.Popen([sys.executable, "-B", "-m", "pytest", "--assert=plain", "-xq",
                    "tests/fake/test_quarantine_process_world.py::TestProcessWorld",
                    "--hypothesis-seed=131"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                    "SF_WORLD_SCENARIO": scenario, "SF_WORLD_CONSUMER": consumer,
                                    "SF_WORLD_EXAMPLES": os.environ.get("SF_WORLD_HISTORY_EXAMPLES", "30"),
                                    "SF_WORLD_DEADLINE_MS": "30000"}) as child:
                try:
                    stdout, stderr = child.communicate(timeout=min(540, max(.001, deadline - time.monotonic())))
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
            if expect_failure:
                detected = result.returncode == 1 and assertion in result.stdout and minimized
                found += int(detected)
                label = "FOUND" if detected else "MISSED"
            else:
                detected = result.returncode == 0 and bool(re.search(r"\b[1-9]\d* passed\b", result.stdout))
                quiet += int(detected)
                label = "QUIET" if detected else "FAILED CONTROL"
            outcome = f"{label} {revision} {scenario} {consumer}: {assertion} ({time.monotonic() - started:.1f}s)"
            summary.append(outcome)
            print(outcome, flush=True)
    finally:
        for p, original in originals.items():
            p.write_bytes(original)
            assert p.read_bytes() == original
    outcome = (f"{found}/{len(cases)} historical bugs found; "
               f"{quiet}/{len(controls)} history controls quiet; production bytes restored")
    (evidence / "history-summary.txt").write_text("\n".join([*summary, outcome]) + "\n")
    print(outcome, flush=True)
    return int(found != len(cases) or quiet != len(controls))


def main():
    previous = signal.getsignal(signal.SIGTERM)
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt("history run terminated; restore production source")
    signal.signal(signal.SIGTERM, interrupt)
    try:
        return _run_history()
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
