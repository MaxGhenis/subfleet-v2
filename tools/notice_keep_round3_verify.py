"""Run the requested r3 regression one file at a time, retaining exact commands."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time


FILES = [
    "tests/unit/test_notice_keep.py", "tests/unit/test_retention_outcomes.py",
    "tests/fake/test_notice_keep_delivery.py", "tests/unit/test_policy.py",
    "tests/unit/test_retention_archive.py", "tests/unit/test_retention_archive_properties.py",
    "tests/unit/test_notice_rendering.py", "tests/unit/test_operator_notices.py",
    "tests/fake/test_notice_delivery.py", "tests/fake/test_notice_header.py",
    "tests/unit/test_timers_retention.py", "tests/fake/test_retention_dormant.py",
    "tests/fake/test_state_contract.py", "tests/unit/test_test_module_names.py",
    "tests/unit/test_invariants_index.py", "tests/unit/test_handler_refusals.py",
    "tests/unit/test_pin_notice_identity.py", "tests/unit/test_retention_outcome_pages.py",
    "tests/unit/test_retention_outcome_permissions.py",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", nargs="+", default=FILES)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    scratch = Path(os.environ["TMPDIR"])
    assert "sf-nk-f3" in str(scratch)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    plugin = scratch / "sf_nk_f3_test_plugin.py"
    plugin.write_text('''import os
from pathlib import Path
import pytest
@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    from tests import failed_keep
    failed_keep.ROOT = Path(os.environ["TMPDIR"]) / "sf-nk-f3-failed"
''')
    env = dict(os.environ, SUBFLEET_LIVE="0")
    env["PYTHONPATH"] = str(scratch) + os.pathsep + str(Path.cwd())
    results = []
    for index, file in enumerate(args.files):
        name = f"{args.label}-{index:02d}-{Path(file).stem}"
        workers = "0" if file.endswith("test_retention_outcome_pages.py") else "4"
        command = ["uv", "--cache-dir", os.environ["UV_CACHE_DIR"], "run", "--no-sync", "--with", "pytest-xdist>=3.6", "pytest", "-q", "-s",
                   "-n", workers, "--timeout=540", "--tb=short", "-p", "sf_nk_f3_test_plugin",
                   "--basetemp", str(scratch / name),
                   "--junitxml", str(args.output_dir / f"{name}.xml"), file]
        print("RUN " + " ".join(command), flush=True)
        started = time.monotonic()
        with (args.output_dir / f"{name}.log").open("w") as log:
            completed = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        elapsed = time.monotonic() - started
        result = {"file": file, "command": command, "exit_code": completed.returncode,
                  "seconds": elapsed, "log": f"{name}.log", "junit": f"{name}.xml"}
        results.append(result)
        (args.output_dir / f"{args.label}-commands.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    raise SystemExit(int(any(result["exit_code"] for result in results)))


if __name__ == "__main__":
    main()
