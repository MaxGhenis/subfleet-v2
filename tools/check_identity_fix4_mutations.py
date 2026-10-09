"""Run round-four fix mutations serially, with passing assertion controls.

Set TMPDIR to a fresh Darwin user temporary directory outside HOME. Run with
uv run --python 3.12 (or 3.14) python tools/check_identity_fix4_mutations.py
--output /path/to/results.json. Exact source bytes are restored after each run.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = (
    ("F4 URI loses percent encoding", "tools/prepare_identity_rollback.py",
     'database.as_uri() + "?mode=rw"', 'f"file:{database}?mode=rw"',
     "tests/fake/test_identity_rollback.py", "test_rollback_uri_repairs_only_the_requested_locked_store"),
    ("F4 output-prefix loop probes skipped", "subfleet/daemon.py",
     "    absolute = path.absolute()\n    prefix = Path(absolute.anchor)",
     "    return\n    absolute = path.absolute()\n    prefix = Path(absolute.anchor)",
     "tests/fake/test_canonical_identity_fix4.py",
     "test_submit_checks_prefixes_when_nonstrict_resolve_suppresses_loop"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    temporary = Path(os.environ["TMPDIR"]).resolve()
    if not temporary.is_dir() or "tmp" in temporary.parts or temporary.is_relative_to(Path.home()):
        raise ValueError("TMPDIR must exist outside HOME without a tmp path component")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           "HYPOTHESIS_STORAGE_DIRECTORY": str(temporary / "fix4-mutations-hypothesis")}
    records = []
    for name, relative, before, after, test, selector in MUTATIONS:
        path = ROOT / relative
        original = path.read_bytes()
        source = original.decode()
        if source.count(before) != 1:
            raise ValueError(f"{name}: expected one replacement site")
        changed = source.replace(before, after)
        ast.parse(changed, filename=relative)
        command = [sys.executable, "-B", "-m", "pytest", "-q", "-x",
                   "-p", "no:cacheprovider", "-p", "tests.canonical_identity_privacy", test, "-k", selector]

        def run():
            return subprocess.run(command, cwd=ROOT, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        control = run()
        record = {"name": name, "path": relative, "test": test, "selector": selector,
                  "control_returncode": control.returncode, "control_output": control.stdout}
        if control.returncode:
            record["verdict"] = "CONTROL FAILED"
        else:
            try:
                path.write_bytes(changed.encode())
                mutant = run()
                assertion = bool(re.search(r"AssertionError|^E\s+Failed: |^E\s+assert ", mutant.stdout, re.M))
                verdict = ("KILLED BY ASSERTION" if mutant.returncode == 1 and assertion
                           else "SURVIVED" if mutant.returncode == 0 else "INVALID FAILURE")
                record.update(mutant_returncode=mutant.returncode, assertion_evidence=assertion,
                              mutant_output=mutant.stdout, verdict=verdict)
            finally:
                path.write_bytes(original)
                assert path.read_bytes() == original
        records.append(record)
        print(f"{name}: {record['verdict']}", flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"python": sys.version, "results": records}, indent=2) + "\n")
    return int(any(record["verdict"] != "KILLED BY ASSERTION" for record in records))


if __name__ == "__main__":
    raise SystemExit(main())
