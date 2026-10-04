"""PR 127 review mutations; foreground children, bounded, always restored."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = [
    ("ci-environment-pins", "tests/unit/test_claude_conversation_mcp_scope.py",
     '        "SUBFLEET_TURN_JOB": "turn-job", "SUBFLEET_SESSION_ID": SESSION,\n', '',
     "tests/unit/test_claude_conversation_mcp_scope.py"),
    ("ci-ledger", "docs/desktop/ledger.json", '"C-24.10"', '"C-24.1"',
     "tests/unit/test_desktop_ledger.py::test_milestone_9_clauses_are_all_cited"),
    ("ci-history", "subfleet/conversations/history.py",
     'if path is None and conversation["provider"] == "claude" and conversation.get("workspace"):',
     'if False and path is None and conversation["provider"] == "claude" and conversation.get("workspace"):',
     "tests/unit/test_review_pr127_fixes.py::test_history_before_first_catalog_pass"),
]


def run_case(case):
    name, relative, before, after, node = case
    path = ROOT / relative
    original = path.read_text()
    assert original.count(before) == 1, (name, "anchor must be unique")
    try:
        path.write_text(original.replace(before, after))
        env = {**os.environ, "PYTHONPYCACHEPREFIX": str(ROOT / "build/review/pycache" / name)}
        child = subprocess.Popen([sys.executable, "-m", "pytest", "-q", "--tb=short", node],
                                 cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, start_new_session=True)
        try:
            out, _ = child.communicate(timeout=180)
        except BaseException:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise
        killed = child.returncode == 1 and "failed" in out
        result = {"mutation": name, "test": node, "killed": killed,
                  "result": out.strip().splitlines()[-1], "output": out}
        print(json.dumps({k: v for k, v in result.items() if k != "output"}), flush=True)
        return result
    finally:
        path.write_text(original)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--report", default="docs/reports/2026-10-04-continuation-review-mutations.json")
    args = parser.parse_args()
    cases = [c for c in MUTATIONS if not args.only or c[0] in args.only]
    assert cases
    results = [run_case(c) for c in cases]
    report = ROOT / args.report
    previous = json.loads(report.read_text()) if report.exists() else []
    names = {r["mutation"] for r in results}
    report.write_text(json.dumps([r for r in previous if r["mutation"] not in names] + results, indent=2) + "\n")
    return int(not all(r["killed"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
