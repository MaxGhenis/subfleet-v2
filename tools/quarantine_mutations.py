"""Foreground C-5.7 mutation checks; restore production bytes after every run.

Usage: .venv/bin/python tools/quarantine_mutations.py
Uses only state fixtures (no daemon/provider subprocesses); each pytest slice
has a nine-minute bound. Output is a concise summary, never a repo artifact.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FAKE = "tests/fake/test_quarantine_self_resolve.py::"
MUTATIONS = (
    ("unverifiable census accepted", "subfleet/procs.py",
     "return not self.unverifiable and not self.errors and not self.live_pids", "return not self.live_pids",
     FAKE + "test_live_or_unverifiable_census_never_releases_across_many_paces[True]"),
    ("PID reuse ignored", "subfleet/procs.py",
     "elif table[pid][3] != known.proc_start:", "elif False:",
     FAKE + "test_pid_reuse_with_different_start_time_counts_as_gone"),
    ("leases retained after release", "subfleet/daemon.py",
     'tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (a["job_id"], a["attempt_id"]))',
     'tx.execute("DELETE FROM leases WHERE 0 AND holder IN (?,?)", (a["job_id"], a["attempt_id"]))',
     FAKE + "test_writer_exit_frees_every_lease_saves_salvage_and_admits_waiting_turn"),
    ("durable pace disabled", "subfleet/daemon.py",
     '(quarantine_time(self.policy.get("quarantine_recheck_s", QUARANTINE_RECHECK_S)), a["attempt_id"]))\n        census = self._contain(a)',
     '(quarantine_time(0), a["attempt_id"]))\n        census = self._contain(a)',
     FAKE + "test_live_or_unverifiable_census_never_releases_across_many_paces[False]"),
    ("salvage receipt ignored", "subfleet/daemon.py",
     "receipt = self._read_json(receipt_path)\n        if receipt is None:",
     "receipt = None\n        if receipt is None:",
     FAKE + "test_restart_mid_resolution_resumes_without_double_salvage[quarantine-saved]"),
)


def main():
    killed = 0
    for name, filename, old, new, node in MUTATIONS:
        path = ROOT / filename
        original = path.read_text()
        assert original.count(old) == 1, (name, original.count(old))
        try:
            path.write_text(original.replace(old, new))
            result = subprocess.run([sys.executable, "-m", "pytest", "-xq", node], cwd=ROOT,
                                    capture_output=True, text=True, timeout=540)
            # Infrastructure errors are not mutation kills: require an actual
            # test assertion failure and pytest's tests-failed exit status.
            detected = result.returncode == 1 and "AssertionError" in result.stdout
            if not detected:
                print(result.stdout[-6000:], result.stderr[-2000:], flush=True)
            assert detected, f"mutation survived or failed to run: {name}"
            killed += 1
            print(f"KILLED: {name}: {result.stdout.strip().splitlines()[-1]}", flush=True)
        finally:
            path.write_text(original)
            assert path.read_text() == original
    print(f"{killed}/{len(MUTATIONS)} mutations killed; production source restored", flush=True)


if __name__ == "__main__":
    main()
