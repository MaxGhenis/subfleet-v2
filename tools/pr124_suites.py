"""Foreground slices for the requested daemon, frontend and app-protocol suites."""
import json
from pathlib import Path
import subprocess
import sys

from tools.pr124_verify import EVIDENCE, ROOT, run

PYTHON = str(ROOT / ".venv/bin/python")


def pytest(name, nodes):
    return run(name, 580, [PYTHON, "-m", "pytest", "-p", "tools.app_cutover_pytest", "-q", *nodes])


def frontend():
    collected = subprocess.run([PYTHON, "-m", "pytest", "tests/frontend", "--collect-only", "-q"],
                               cwd=ROOT, check=True, capture_output=True, text=True)
    nodes = [line for line in collected.stdout.splitlines() if line.startswith("tests/frontend/") and "::" in line]
    groups = {}
    for node in nodes:
        file = node.split("::")[0]
        # UI and core use different compiles: never put both in the first slice.
        group = file
        if "test_app_cutover_start.py" in node and "retry_preserves_ids" in node:
            group += "-core"
        if "test_core_steer_properties.py" in node:
            group = node
        groups.setdefault(group, []).append(node)
    results = []
    for index, tests in enumerate(groups.values(), 1):
        results.append(pytest(f"suite-frontend-{index:02d}", tests))
    (EVIDENCE / "frontend-suite.json").write_text(json.dumps({"collected": len(nodes), "slices": results}, indent=2) + "\n")


def catalog():
    pytest("suite-catalog", ["tests/unit/test_conversation_catalog.py"])
    pytest("suite-catalog-lifecycle", ["tests/unit/test_conversation_catalog_lifecycle.py"])
    pytest("suite-status-json", ["tests/unit/test_status_json.py"])


if __name__ == "__main__":
    if sys.argv[1] == "frontend":
        frontend()
    elif sys.argv[1] == "daemon":
        pytest("suite-conversation-service", ["tests/unit/test_conversation_service.py"])
        catalog()
        pytest("suite-app-cutover-daemon", ["tests/unit/test_app_cutover_daemon.py", "tests/unit/test_pr124_fixes.py"])
    elif sys.argv[1] == "catalog":
        catalog()
    else:
        raise SystemExit("expected daemon, catalog or frontend")
