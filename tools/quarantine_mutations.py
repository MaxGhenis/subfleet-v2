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
ROUND2 = "tests/fake/test_review_pr131_round2.py::"
MUTATIONS = (
    ("saved lineage identities ignored", "subfleet/procs.py",
     "        for known in lineage_roots:\n            records_by_pid.setdefault(known.pid, []).append(known.identity)",
     "        for known in ():\n            records_by_pid.setdefault(known.pid, []).append(known.identity)",
     "tests/unit/test_procs.py::test_saved_identity_follows_a_writer_after_it_changes_group"),
    ("saved lineage groups ignored", "subfleet/procs.py",
     "            groups.update(seen.group(known.pgid))", "            pass  # mutation: discard saved groups",
     "tests/fake/test_quarantine_detached_writers.py::test_observed_detached_lineage_and_group_survive_parent_exit[False-inspection]"),
    ("cwd writer excluded from live census", "subfleet/procs.py",
     "return self.group_pids | self.descendant_pids | self.marker_pids | self.cwd_pids",
     "return self.group_pids | self.descendant_pids | self.marker_pids",
     "tests/unit/test_procs.py::test_cwd_only_writer_participates_in_verified_empty"),
    ("partial markers discarded", "subfleet/procs.py",
     "if marker.search(command) or (root_marker is not None and root_marker.search(command)):",
     "if marker.search(command) and (root_marker is None or root_marker.search(command)):",
     "tests/unit/test_procs.py::test_either_exact_marker_holds_without_disclosing_environment"),
    ("legacy owned provider cannot discharge", "subfleet/daemon.py",
     'and not evidence.get("provider_identities") and not owned_provider',
     'and not evidence.get("provider_identities")',
     "tests/fake/test_quarantine_detached_writers.py::test_legacy_kill_owned_provider_discharges_without_force[False]"),
    ("lineage overflow silently releases", "subfleet/procs.py",
     "if lineage_overflow_boot is not None and not rebooted(lineage_overflow_boot):",
     "if False and lineage_overflow_boot is not None and not rebooted(lineage_overflow_boot):",
     "tests/fake/test_quarantine_detached_writers.py::test_lineage_limit_keeps_newest_and_overflow_holds_until_proven_reboot"),
    ("corrupt diagnostic boots accepted as ownership proof", "subfleet/daemon.py",
     '        if isinstance(observed, list):\n'
     '            boots.update(boot for boot in observed if isinstance(boot, str))',
     '        if not isinstance(observed, list) or any(not isinstance(boot, str) for boot in observed):\n'
     '            raise ValueError("corrupt writer lineage boot evidence")\n'
     '        boots.update(observed)',
     "tests/fake/test_quarantine_review_fixes.py::test_corrupt_diagnostic_boot_observations_do_not_pin_an_empty_census"),
    ("same-boot reboot gate restored", "subfleet/procs.py",
     "        roots_rebooted = rebooted(launch_boot_id)",
     '        if any(not rebooted(known) for known in lineage_boot_ids):\n'
     '            errors.append("writer lineage requires a proven reboot")\n'
     '        roots_rebooted = rebooted(launch_boot_id)',
     ROUND2 + "test_empty_same_boot_census_releases_within_one_pace_after_restart"),
    ("marker scan ignored", "subfleet/procs.py",
     "if marker.search(command) or (root_marker is not None and root_marker.search(command)):",
     "if False and (marker.search(command) or (root_marker is not None and root_marker.search(command))):",
     ROUND2 + "test_forked_child_keeping_markers_holds_across_many_paces_after_parent_exits"),
    ("missing child publication accepted", "subfleet/procs.py",
     "if child_unrecorded and not providers and not roots_rebooted:", "if False and child_unrecorded and not providers and not roots_rebooted:",
     ROUND2 + "test_real_legacy_start_format_holds_unobserved_child_after_guardian_dies[False-False]"),
    ("retained group descendant roots omitted", "subfleet/procs.py",
     "| owned | groups)", "| owned)",
     ROUND2 + "test_census_walks_descendants_of_a_recycled_orphan_group_member"),
    ("pre-reboot group roots retained", "subfleet/procs.py",
     "groups = set() if roots_rebooted else set(seen.group(pgid))", "groups = set(seen.group(pgid))",
     ROUND2 + "test_pre_reboot_roots_do_not_own_new_boot_processes[False-group]"),
    ("unverifiable census accepted", "subfleet/procs.py",
     "return not self.unverifiable and not self.errors and not self.live_pids", "return not self.live_pids",
     FAKE + "test_live_or_unverifiable_census_never_releases_across_many_paces[True]"),
    ("PID reuse ignored", "subfleet/procs.py",
     "if table[pid][3] != known.proc_start:", "if False:",
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
    selected = MUTATIONS if len(sys.argv) == 1 else tuple(m for m in MUTATIONS if m[0] in sys.argv[1:])
    assert selected, "no mutations selected"
    killed = 0
    for name, filename, old, new, node in selected:
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
    print(f"{killed}/{len(selected)} mutations killed; production source restored", flush=True)


if __name__ == "__main__":
    main()
