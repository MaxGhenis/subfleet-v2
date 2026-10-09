"""Serial independent mutations of the process model's own assertions.

Run only when no other pytest process is active, with a fresh caller-owned
TMPDIR. Restore exact model bytes after each mutant; no production edits.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "tests/fake/test_quarantine_process_world.py"
MUTATIONS = (
    ("S1 ignores writers after markers disappear",
     "if p.writer and not p.zombie]",
     "if p.writer and not p.zombie and p.marked]",
     "tests/fake/test_quarantine_process_world.py::test_unconditional_s1_has_the_documented_invisible_writer_counterexample"),
    ("S2 accepts confirmed foreign-group identities",
     "p.pgid == 100 or (pid, p.start) in self.owned",
     "True",
     "tests/fake/test_review_pr131_round7_model.py::test_s2_oracle_rejects_foreign_group_signal"),
    ("S2 rejects a confirmed previously owned escape",
     "p.pgid == 100 or (pid, p.start) in self.owned",
     "p.pgid == 100",
     "tests/fake/test_review_pr131_round7_model.py::test_previously_owned_escape_is_a_valid_signal_target[attempt]"),
)


def run(node):
    for cache in (ROOT / "tests/fake/__pycache__").glob("test_quarantine_process_world.*.pyc"):
        cache.unlink()
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "--assert=plain", "-q", node],
        cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=540)


def main():
    original = MODEL.read_bytes()
    source = original.decode()
    before = hashlib.sha256(original).hexdigest()
    killed = 0
    try:
        for name, old, new, node in MUTATIONS:
            assert source.count(old) == 1, (name, source.count(old))
            control = run(node)
            print(f"CONTROL {name}: exit {control.returncode}", flush=True)
            print(control.stdout, flush=True)
            assert control.returncode == 0, control.stderr
            try:
                MODEL.write_text(source.replace(old, new))
                mutant = run(node)
                detected = mutant.returncode == 1 and any(
                    label in mutant.stdout for label in ("DID NOT RAISE", "AssertionError"))
                print(f"{'KILLED' if detected else 'MISSED'} {name}: exit {mutant.returncode}", flush=True)
                print(mutant.stdout, mutant.stderr, flush=True)
                killed += int(detected)
            finally:
                MODEL.write_bytes(original)
                assert MODEL.read_bytes() == original
    finally:
        MODEL.write_bytes(original)
    after = hashlib.sha256(MODEL.read_bytes()).hexdigest()
    print(f"{killed}/{len(MUTATIONS)} own-oracle mutations killed", flush=True)
    print(f"model SHA-256 before/after: {before} / {after}", flush=True)
    return int(killed != len(MUTATIONS))


if __name__ == "__main__":
    sys.exit(main())
