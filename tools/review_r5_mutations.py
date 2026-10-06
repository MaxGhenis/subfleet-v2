"""PR #127 r5 mutation checks; foreground tests with bounded owned children."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TESTS = "tests/unit/test_review_r5_repro.py::"
MUTATIONS = [
    ("omit-completion-intent", "store.py", "if changed and final_wake is not None:",
     "if False and changed and final_wake is not None:",
     "test_final_timer_is_recovered_after_settlement_restart[after-complete]"),
    ("skip-intent-recovery", "service.py", "self.wakes.replay_final()", "pass",
     "test_final_timer_is_recovered_after_settlement_restart[after-complete]"),
    ("expire-timer-during-downtime", "wakes.py",
     'settled_at = intent["settled_at"] if intent else self.now()',
     "settled_at = self.now()",
     "test_final_wake_intent_survives_each_registration_boundary[overdue-restart]"),
]


def run_case(case):
    name, filename, before, after, test = case
    source = ROOT / "subfleet/conversations" / filename
    original = source.read_text()
    assert original.count(before) == 1, (name, "anchor must be unique")
    artifacts = ROOT / ".fix-tmp"
    artifacts.mkdir(exist_ok=True)
    env = {**os.environ, "TMPDIR": str(artifacts) + "/",
           "GIT_CEILING_DIRECTORIES": str(artifacts), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPYCACHEPREFIX", None)
    env.pop("PYTHONPATH", None)
    try:
        source.write_text(original.replace(before, after))
        # Prevent a restored source's timestamp from reusing a mutant's bytecode.
        for cached in (source.parent / "__pycache__").glob(source.stem + ".*.pyc"):
            cached.unlink()
        child = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider",
             f"--junitxml={artifacts / ('mutation-' + name + '.xml')}", TESTS + test],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True)
        try:
            output, _ = child.communicate(timeout=180)
        except BaseException:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate()
            raise
        (artifacts / ("mutation-" + name + ".log")).write_text(output)
        summary = output.strip().splitlines()[-1]
        killed = child.returncode == 1 and bool(re.search(r"\b[1-9]\d* failed\b", summary)) and "error" not in summary
        print(json.dumps({"mutation": name, "test": TESTS + test,
                          "killed": killed, "summary": summary}), flush=True)
        return killed
    finally:
        source.write_text(original)


def main():
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    selected = sys.argv[1:]
    assert all(name in {case[0] for case in MUTATIONS} for name in selected), selected
    cases = [case for case in MUTATIONS if not selected or case[0] in selected]
    return int(not all([run_case(case) for case in cases]))


if __name__ == "__main__":
    raise SystemExit(main())
