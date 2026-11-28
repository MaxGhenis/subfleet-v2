"""PR #127 r4 mutation checks: foreground tests, bounded, source always restored."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TESTS = "tests/unit/test_review_r4_repro.py::"
MUTATIONS = [
    ("reset-retained-window", "windows.get(p, created_at)", "created_at",
     "test_unobserved_event_survives_rearm"),
    ("extend-old-window-to-new-target", "windows.get(p, created_at)",
     "min(windows.values(), default=created_at)",
     "test_retained_target_window_does_not_extend_to_new_targets"),
    ("skip-events-after-refusal", '(p not in before or before[p].get("error"))',
     "(p not in before)", "test_event_after_refusal_is_delivered_when_access_returns"),
    ("drop-ready-refusal", "if target in ready:",
     'if target in ready and not snapshot.get("error"):',
     "test_unannounced_refusal_survives_rearm"),
    ("record-superseded-refusal", "if not updated.rowcount:",
     "if False and not updated.rowcount:",
     "test_superseded_poll_cannot_suppress_an_undelivered_refusal"),
]


def run_case(case):
    name, before, after, test = case
    source = ROOT / "subfleet/conversations/wakes.py"
    original = source.read_text()
    assert original.count(before) == 1, (name, "anchor must be unique")
    artifacts = ROOT / ".fix-tmp"
    artifacts.mkdir(exist_ok=True)
    env = {**os.environ, "TMPDIR": str(artifacts) + "/", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPYCACHEPREFIX", None)
    try:
        source.write_text(original.replace(before, after))
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
        result = {"mutation": name, "test": TESTS + test, "killed": killed, "summary": summary}
        print(json.dumps(result), flush=True)
        return killed
    finally:
        source.write_text(original)


def main():
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    selected = sys.argv[1:]
    assert all(name in {case[0] for case in MUTATIONS} for name in selected), selected
    cases = [case for case in MUTATIONS if not selected or case[0] in selected]
    killed = [run_case(case) for case in cases]
    return int(not all(killed))


if __name__ == "__main__":
    raise SystemExit(main())
