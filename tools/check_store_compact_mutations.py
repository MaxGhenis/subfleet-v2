"""Run the four offline D-STORE-FREE1 mutants, restoring exact source bytes.

Use the development interpreter from the worktree. All test stores and logs
live below the explicit scratch root. Do not run concurrently with a process
which is still importing compact_store; already imported code is unaffected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.scratch_root.mkdir(parents=True, exist_ok=True)
    source = Path("subfleet/compact_store.py")
    pristine = source.read_bytes()
    saved = args.scratch_root / "compact_store.pristine.py"
    saved.write_bytes(pristine)
    original = pristine.decode()
    mutants = [
        ("digest", original.replace("if after != before:", "if False:  # mutant: omit digest proof"),
         "test_valid_output_with_changed_value_is_never_published"),
        ("rename-before-fsync", original.replace("            _fsync_file(temporary)\n", "            # mutant: publish before output fsync\n")
         .replace("            temporary = None\n            stage(\"replaced\")", "            _fsync_file(database)\n            temporary = None\n            stage(\"replaced\")"),
         "test_temp_database_is_fsynced_before_rename"),
        ("daemon-check", original.replace("with _daemon_lock(root):", "with contextlib.nullcontext():"),
         "test_daemon_lock_refusal_changes_no_byte"),
        ("auto-vacuum", original.replace('connection.execute("PRAGMA auto_vacuum=INCREMENTAL")',
                                         'connection.execute("PRAGMA auto_vacuum=NONE")'),
         "test_compact_preserves_every_table_and_index_and_enables_incremental"),
    ]
    evidence = {"pristine_sha256": hashlib.sha256(pristine).hexdigest(), "mutants": []}
    try:
        for name, changed, test in mutants:
            if changed == original:
                raise RuntimeError(f"mutant {name} made no change")
            source.write_text(changed)
            argv = [sys.executable, "-m", "pytest", "-q", "tests/unit/test_compact_store.py",
                    "-k", test, "-x", "--tb=short", "--timeout=120", "--basetemp",
                    str(args.scratch_root / ("pytest-mutant-" + name))]
            log = args.scratch_root / ("mutant-" + name + ".log")
            try:
                with log.open("w") as output:
                    result = subprocess.run(argv, stdout=output, stderr=subprocess.STDOUT,
                                            timeout=600, env=os.environ.copy())
                entry = {"mutant": name, "test": test, "argv": argv,
                         "returncode": result.returncode, "killed": result.returncode == 1,
                         "log": log.read_text()}
            finally:
                source.write_bytes(saved.read_bytes())
                if source.read_bytes() != pristine:
                    raise RuntimeError("pristine restoration failed")
            evidence["mutants"].append(entry)
            args.output.write_text(json.dumps(evidence, indent=2) + "\n")
            print(name, "KILLED" if entry["killed"] else "SURVIVED", flush=True)
    finally:
        source.write_bytes(pristine)
        evidence["restored_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    return 0 if all(item["killed"] for item in evidence["mutants"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
