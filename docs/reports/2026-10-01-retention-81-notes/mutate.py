"""Mutation check for the fixes to #81's four notes on #76.

Each mutant undoes or weakens one fix by a textual replacement in the
workspace, runs the tests named for it, and restores the file whatever
happens. A mutant is killed when the tests fail. Run from the workspace root.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.cwd()
NOTES = "tests/unit/test_retention_81_notes.py"
ARCHIVE = "tests/unit/test_retention_archive.py"

MUTANTS = [
    ("M1 note 1: unchanged() ignores ctime again for a file that had other links",
     "subfleet/retention_fs.py",
     'return _same_but_ctime(recorded, st) and st.st_ctime_ns == recorded["ctime"]',
     'return _same_but_ctime(recorded, st) and (recorded.get("nlink", 1) > 1 or st.st_ctime_ns == recorded["ctime"])',
     [NOTES + "::test_a_hard_linked_file_rewritten_with_its_mtime_put_back_is_not_deleted",
      NOTES + "::test_verified_deletion_unlinks_only_the_bytes_the_archive_holds"]),
    ("M2 note 1: still_archived trusts a ctime-only move without comparing the bytes",
     "subfleet/retention_fs.py",
     'return digest == entry["sha256"] and same_content_signature(before, os.fstat(fd))',
     'return same_content_signature(before, os.fstat(fd))',
     [NOTES + "::test_a_hard_linked_file_rewritten_with_its_mtime_put_back_is_not_deleted",
      NOTES + "::test_verified_deletion_unlinks_only_the_bytes_the_archive_holds"]),
    ("M3 note 1: verified deletion strict on ctime (no content check: the over-strict fix)",
     "subfleet/retention_fs.py",
     'if not still_archived(record, st, fd, name, self.check):',
     'if not unchanged(record["sig"], st):',
     [NOTES + "::test_a_hard_link_whose_ctime_moved_only_because_its_sibling_went_is_deleted",
      NOTES + "::test_verified_deletion_unlinks_only_the_bytes_the_archive_holds",
      ARCHIVE + "::test_interrupted_removal_resumes_and_leaves_no_half_tree"]),
    ("M4 note 1: the final check accepts a ctime-only move without the bytes",
     "subfleet/retention_archive.py",
     'if entry is None or not rfs.still_archived(entry, st, parent, name, self.ctx.check):',
     'if entry is None or not (rfs.unchanged(entry["sig"], st) or rfs._same_but_ctime(entry["sig"], st)):',
     [NOTES + "::test_a_hard_linked_file_rewritten_with_its_mtime_put_back_is_not_deleted"]),
    ("M5 note 1: the content check also for a file that had one link",
     "subfleet/retention_fs.py",
     'if (recorded["t"] != "f" or recorded.get("nlink", 1) < 2 or not entry.get("sha256") or name is None',
     'if (recorded["t"] != "f" or not entry.get("sha256") or name is None',
     [NOTES + "::test_verified_deletion_unlinks_only_the_bytes_the_archive_holds"]),
    ("M6 note 2: neither move guard sees a tree another tool holds aside",
     "subfleet/retention_archive.py",
     '            away = tree_away(worktree)\n            if away is not None:',
     '            away = tree_away(worktree)\n            rgit.moved_tree = lambda common, tree: None\n            if False:',
     [NOTES + "::test_a_tree_the_sweep_holds_in_quarantine_keeps_its_job_until_it_is_back"]),
    ("M7 note 2: no presence check between begin and quarantine",
     "subfleet/retention_archive.py",
     'and os.path.lexists(j["worktree"]) != present:',
     'and False:',
     [NOTES + "::test_a_tree_the_sweep_moves_back_before_quarantine_is_not_archived_without_its_registration",
      NOTES + "::test_a_tree_the_sweep_moves_away_after_begin_keeps_its_registration"]),
    ("M7b note 2: the final check does not see a gone tree come back",
     "subfleet/retention_archive.py",
     'if j.get("worktree") and not j["moved"]["worktree"] and os.path.lexists(j["worktree"]):',
     'if False:',
     [NOTES + "::test_a_tree_moved_back_while_its_job_retires_without_it_keeps_the_job"]),
    ("M8 note 2: neither survey move guard sees a tree held aside",
     "subfleet/retention_survey.py",
     '        away = rarch.tree_away(worktree)\n        if away is not None:',
     '        away = rarch.tree_away(worktree)\n        rgit.moved_tree = lambda common, tree: None\n        if False:',
     [NOTES + "::test_the_survey_keeps_a_job_whose_tree_the_sweep_holds"]),
    ("M9 note 3: no unrecorded allocation is owned",
     "subfleet/retention_archive.py",
     '        if not os.path.lexists(unrecorded) and tree_away(unrecorded) is None and not job.get("workdir_head"):\n            return None',
     '        if True:\n            return None',
     [NOTES + "::test_a_tree_admission_allocated_for_a_job_it_never_recorded_retires_with_it"]),
    ("M10 note 3: no worktree-in-use pin for an unrecorded allocation",
     "subfleet/retention.py",
     '    if root is not None:\n        for row in store.query(_UNRECORDED_IN_USE',
     '    if False:\n        for row in store.query(_UNRECORDED_IN_USE',
     [NOTES + "::test_a_job_still_to_run_inside_an_unrecorded_allocation_keeps_it"]),
    ("M11 note 4: the repositories retention knows are not searched",
     "subfleet/retention_archive.py",
     'for common in ([near] if near is not None else []) + list(known()):',
     'for common in ([near] if near is not None else []):',
     [NOTES + "::test_a_gone_workdirs_job_finds_its_registration_through_the_repositories_retention_knows",
      NOTES + "::test_a_gone_workdirs_job_with_salvage_retires_with_its_salvage_bundled"]),
    ("M12 note 4: the workdir's nearest ancestor is not searched",
     "subfleet/retention_archive.py",
     'near = rgit.repository_near(Path(workdir), cancel=cancel) if workdir else None',
     'near = None',
     [NOTES + "::test_a_gone_workdirs_job_finds_its_repository_from_the_workdirs_nearest_ancestor"]),
    ("M13 note 4: no repository by the job's salvage refs",
     "subfleet/retention_archive.py",
     '    if wanted:\n        for common in candidates:',
     '    if False:\n        for common in candidates:',
     [NOTES + "::test_a_gone_workdirs_job_whose_registration_was_pruned_is_found_by_its_salvage_refs"]),
    ("M14 note 4: any known repository taken as the job's (salvage refs not required)",
     "subfleet/retention_archive.py",
     'if all(ref in listing and salvage_digest(listing[ref]) == digest for ref, digest in wanted.items()):',
     'if True:',
     [NOTES + "::test_a_repository_that_does_not_hold_the_jobs_salvage_is_not_taken_for_its_own"]),
    ("M15 note 4: the reason does not say the repository was not found",
     "subfleet/retention_archive.py",
     'f"{ref}: {lost}" if lost else str(ref))',
     'str(ref))',
     [NOTES + "::test_a_job_whose_repository_cannot_be_found_says_so"]),
    ("M16 note 1: a regenerable hardlink is not hashed for a ctime-only move",
     "subfleet/retention_archive.py",
     'if st.st_nlink > 1 and "sha256" not in entry:',
     'if False:',
     [NOTES + "::test_a_regenerable_file_whose_ctime_another_link_moved_is_deleted"]),
    ("M17 note 4: a salvage ref name is accepted without its recorded commit digest",
     "subfleet/retention_archive.py",
     'if all(ref in listing and salvage_digest(listing[ref]) == digest for ref, digest in wanted.items()):',
     'if all(ref in listing for ref in wanted):',
     [NOTES + "::test_a_salvage_ref_of_the_same_name_in_another_repository_is_not_the_jobs"]),
    ("M18 note 1: the digest may hash a different version than the regenerability proof",
     "subfleet/retention_archive.py",
     'if not rfs.same_content_signature(st, before) or not stat.S_ISREG(before.st_mode):',
     'if (before.st_dev, before.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(before.st_mode):',
     [NOTES + "::test_a_regenerable_file_changed_between_its_proof_and_digest_is_kept"]),
    ("M19 note 2: a registration moved outside the sibling quarantine is ignored",
     "subfleet/retention_git.py",
     '    the caller keeps the job and never acts on that checkout or registration.\n    """\n    pattern',
     '    the caller keeps the job and never acts on that checkout or registration.\n    """\n    return None\n    pattern',
     [NOTES + "::test_a_registered_tree_moved_elsewhere_keeps_its_job"]),
    ("M20 note 4: an undiscovered source without salvage retires without its anchor",
     "subfleet/retention_archive.py",
     'if lost and not salvage_rows:',
     'if False:',
     [NOTES + "::test_an_undiscovered_repository_keeps_the_job_without_salvage_until_it_can_be_bundled"]),

    ("M21 notes 2/3: a missing NULL allocation loses its expected path",
     "subfleet/retention_archive.py",
     'if not os.path.lexists(unrecorded) and tree_away(unrecorded) is None and not job.get("workdir_head"):',
     'if not os.path.lexists(unrecorded) and tree_away(unrecorded) is None:',
     [NOTES + "::test_an_unrecorded_allocation_whose_tree_is_gone_bundles_its_registration",
      NOTES + "::test_an_unrecorded_allocation_returning_during_source_lookup_keeps_its_job"]),
]


def main() -> int:
    only = set(sys.argv[1:])
    lines = []
    survivors = 0
    for name, path, old, new, tests in MUTANTS:
        if only and name.split()[0] not in only:
            continue
        file = ROOT / path
        original = file.read_text()
        if original.count(old) != 1:
            lines.append(f"{name}: NOT APPLIED (pattern found {original.count(old)} times)")
            survivors += 1
            continue
        started = time.monotonic()
        shutil.rmtree(ROOT / ".hypothesis", ignore_errors=True)     # no saved example helps a mutant
        try:
            file.write_text(original.replace(old, new))
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests],
                                    capture_output=True, text=True, timeout=900)
        finally:
            file.write_text(original)
        killed = result.returncode != 0
        survivors += not killed
        tail = [l for l in result.stdout.splitlines() if l.startswith(("FAILED", "ERROR")) or " passed" in l
                or " failed" in l][-2:]
        lines.append(f"{name}: {'KILLED' if killed else 'SURVIVED'} ({time.monotonic() - started:.0f} s) "
                     + " | ".join(tail))
        print(lines[-1], flush=True)
    print(f"\n{len(lines) - survivors} killed, {survivors} survived or not applied")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
