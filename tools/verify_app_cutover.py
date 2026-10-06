"""Foreground suite, regression and mutation evidence for the 2.1.10 cutover.

Run with the checkout's test interpreter. All copies and logs stay in build/;
the working source, shared git metadata and live app/daemon are untouched.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
from io import BytesIO

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "build/app-cutover-evidence"
GIT = ["git", "--git-dir=" + str(ROOT / ".git-local")] if (ROOT / ".git-local").is_dir() else ["git"]


def run_tests(folder, name, tests):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")}
    env["PYTHONPATH"] = str(folder)
    env["SWIFT_MODULECACHE_PATH"] = "/private/tmp/subfleet-app-cutover-swift-cache"
    env["CLANG_MODULE_CACHE_PATH"] = "/private/tmp/subfleet-app-cutover-clang-cache"
    env["SF_CUTOVER_PROBE_CACHE"] = str(EVIDENCE / "probes")
    start = time.monotonic()
    log = EVIDENCE / (name + ".log")
    with log.open("w") as out:
        result = subprocess.run([sys.executable, "-m", "pytest", "-p", "tools.app_cutover_pytest", "-q", *tests], cwd=folder, env=env,
                                stdout=out, stderr=subprocess.STDOUT)
    seconds = round(time.monotonic() - start, 2)
    tail = log.read_text().splitlines()[-4:]
    summary = {"name": name, "exit_code": result.returncode, "seconds": seconds, "tail": tail}
    print(json.dumps(summary), flush=True)
    return summary


def copy_files(folder, names):
    for name in names:
        source, target = ROOT / name, folder / name
        if not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def baseline():
    folder = EVIDENCE / "baseline"
    folder.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run([*GIT, "archive", "7bfda766"], cwd=ROOT, check=True, capture_output=True).stdout
    with tarfile.open(fileobj=BytesIO(archive)) as files:
        files.extractall(folder, filter="data")
    copy_files(folder, ["tests/unit/test_app_cutover_daemon.py", "tests/frontend/test_core_app_cutover_reading.py",
                        "tests/frontend/test_status_model.py", "tools/app_cutover_pytest.py"])
    summaries = [run_tests(folder, "baseline-daemon", ["tests/unit/test_app_cutover_daemon.py"]),
                 run_tests(folder, "baseline-sandbox", ["tests/unit/test_conversation_service.py::test_a_request_that_reaches_its_text_after_the_service_closed_writes_nothing",
                                                        "tests/unit/test_conversation_catalog_lifecycle.py::test_closing_the_daemon_stops_its_catalog_run_and_the_removed_root_stays_gone"]),
                 run_tests(folder, "baseline-behavior", ["tests/frontend/test_core_app_cutover_reading.py",
                                                        "tests/frontend/test_status_model.py"])]
    # New typed APIs require the new probes. Attempt their tests against the
    # old app as well; its absent probe mode and typed APIs are setup errors.
    copy_files(folder, ["tests/frontend/conftest.py", "tests/frontend/CoreProbe.swift",
                        "tests/frontend/swift.py", "tests/frontend/CoreProbeScenarios.swift", "tests/frontend/ConversationViewProbe.swift",
                        "tests/frontend/CutoverModelProbe.swift",
                        "tests/frontend/CutoverViewScenarios.swift", "tests/frontend/test_app_cutover_start.py",
                        "tests/frontend/test_conversation_view.py"])
    summaries.extend(baseline_additive(folder))
    return summaries


def baseline_additive(folder=None):
    folder = folder or EVIDENCE / "baseline"
    return [run_tests(folder, "baseline-additive-model", ["tests/frontend/test_app_cutover_start.py", "-k", "not retry"]),
            run_tests(folder, "baseline-additive-retry", ["tests/frontend/test_app_cutover_start.py", "-k", "retry"]),
            run_tests(folder, "baseline-additive-views", ["tests/frontend/test_conversation_view.py", "-k", "cutover"])]


def mutations():
    folder = EVIDENCE / "mutations"
    folder.mkdir(parents=True, exist_ok=True)
    names = subprocess.run([*GIT, "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                           check=True, capture_output=True, text=True).stdout.splitlines()
    copy_files(folder, names)
    specs = [
        ("mutation-folder", "subfleet/conversations/service.py",
         "\n        self._check_workspace(provider, workspace, settings)\n", "\n        pass  # mutation: bypass folder policy\n",
         ["tests/unit/test_app_cutover_daemon.py::test_workspace_check_refuses_protected_home_without_writing"]),
        ("mutation-activity", "subfleet/conversations/service.py",
         '        mtime = activity.get((conversation["provider"], canonical_native(native))) if native else None\n',
         "        mtime = None  # mutation: ignore native activity\n",
         ["tests/unit/test_app_cutover_daemon.py::test_last_activity_uses_the_last_catalog_run_before_sorting_and_limiting"]),
        ("mutation-auto", "app/Sources/StatusModel.swift",
         '    return claude > 0 ? "claude" : "codex"\n',
         '    return (counts["codex"] ?? 0) > claude ? "codex" : "claude"\n',
         ["tests/frontend/test_status_model.py::test_auto_provider_follows_the_lanes_ready_now"]),
        ("mutation-task-notice", "app/Sources/TaskNotification.swift",
         "        guard let body = capture(", "        guard false, let body = capture(",
         ["tests/frontend/test_core_app_cutover_reading.py::test_claude_task_completion_is_a_system_notice_with_summary_status_and_exit"]),
    ]
    summaries = []
    for name, file, before, after, tests in specs:
        path = folder / file
        original = path.read_text()
        assert original.count(before) == 1, (file, before)
        try:
            path.write_text(original.replace(before, after))
            summaries.append(run_tests(folder, name, tests))
        finally:
            path.write_text(original)
    return summaries


def frontend():
    """Run every collected frontend test in small, awaited slices."""
    collected = subprocess.run([sys.executable, "-m", "pytest", "tests/frontend", "--collect-only", "-q"],
                               cwd=ROOT, check=True, capture_output=True, text=True)
    nodes = [line for line in collected.stdout.splitlines() if line.startswith("tests/frontend/") and "::" in line]
    assert nodes, collected.stdout
    ordinary = [node for node in nodes if "test_core_steer_properties.py::" not in node]
    slices = [ordinary[index:index + 24] for index in range(0, len(ordinary), 24)]
    slices.extend([[node] for node in nodes if "test_core_steer_properties.py::" in node])
    print(json.dumps({"collected": len(nodes), "slices": len(slices)}), flush=True)
    return [run_tests(ROOT, f"frontend-{index:02d}", tests) for index, tests in enumerate(slices, 1)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["baseline", "baseline-additive", "mutations", "frontend"])
    args = parser.parse_args()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    summaries = {"baseline": baseline, "baseline-additive": baseline_additive,
                 "mutations": mutations, "frontend": frontend}[args.mode]()
    (EVIDENCE / (args.mode + ".json")).write_text(json.dumps(summaries, indent=2) + "\n")
    expected_success = args.mode == "frontend"
    if any((s["exit_code"] == 0) != expected_success or s["seconds"] >= 600 for s in summaries):
        raise SystemExit("An unexpected test result or slice over ten minutes; inspect its log.")


if __name__ == "__main__":
    main()
