"""Fix round 3 mutants: apply one, run the new tests, restore the source and check its hash.

Usage (from the repository root): python3 mutant.py NAME
Writes fix-r3/mutant-NAME.log (through run.sh) and appends one JSON line to fix-r3/mutants.jsonl.
"""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]

MUTANTS = {
    # Keep any recorded alias that resolves, at whatever ID (fix round 2's rule, made total).
    "keep-any-resolvable-alias": ("subfleet/daemon.py",
        '        if isinstance(entry, dict) and entry.get("id") == model_id:\n            return recorded\n',
        '        if isinstance(entry, dict):\n            return recorded\n'),
    # Never keep the alias (the base's rule: round 2's bug returns).
    "never-keep-alias": ("subfleet/daemon.py",
        '        if isinstance(entry, dict) and entry.get("id") == model_id:\n            return recorded\n',
        '        if False:\n            return recorded\n'),
    # The resume refusal asks for a pin again.
    "resume-fix-dropped": ("subfleet/daemon.py",
        "fix=refusals.RESUME_PIN_FIX if resume is not None\n",
        "fix=None if resume is not None\n"),
    # The wire ignores the resume marker.
    "render-ignores-marker": ("subfleet/refusals.py",
        '    if reason == "unknown-pinned-model" and details.get(RESUME_PIN) is True:\n        fix = RESUME_PIN_FIX\n',
        ''),
}

SELECT = ("imported_resume_runs or after_a_policy_change or unknown_pinned_model_when or aliases_share "
          "or resume_refused_for_its_source or most_specific_checked_form")


def main(name: str) -> int:
    rel, old, new = MUTANTS[name]
    path = ROOT / rel
    original = path.read_bytes()
    before = hashlib.sha256(original).hexdigest()
    text = original.decode()
    assert text.count(old) == 1, (name, text.count(old))
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        path.write_text(text.replace(old, new))
        run = subprocess.run([str(HERE / "run.sh"), f"mutant-{name}", "-n", "4", "-q", "-rfE",
                              "tests/fake/test_resume_contract.py", "tests/unit/test_handler_refusals.py",
                              "-k", SELECT], cwd=ROOT, capture_output=True, text=True)
    finally:
        path.write_bytes(original)
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert after == before, (name, before, after)
    summary = [line for line in run.stdout.splitlines() if " passed" in line or " failed" in line][-1:]
    failed = [line.split(" ", 1)[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")]
    record = {"mutant": name, "file": rel, "started": started, "exit": run.returncode,
              "summary": summary, "failed": failed, "sha256_before": before, "sha256_restored": after,
              "killed": run.returncode != 0}
    with open(HERE / "mutants.jsonl", "a") as out:
        out.write(json.dumps(record, sort_keys=True) + "\n")
    print(json.dumps(record, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
