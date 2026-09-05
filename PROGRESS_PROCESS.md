# Process and salvage progress

## State
Process ownership and guardian publication implemented; salvage is implemented
and has passed its first targeted tests.

## Done
- Read C-4, C-5, C-8 and C-13 and shared seams, plans and specified v1 behavior.
- Fixed the public API with the daemon implementer without changing shared seams.
- Implemented recorded boot/start identities, guarded signals, and three-source
  containment with identities only in evidence (no environment snapshots).
- Implemented detached guardian, start/exit receipts, commit-before-launch pipe
  gate, and temp/file-fsync/rename/directory-fsync publication.
- Tested identity mismatches, failed census sources, zombies, signal guards,
  environment-only credentials, receipt order, gate EOF and ENOSPC publication.

## Next
- Commit the independently tested temporary-index salvage implementation.
- Integrate with daemon recovery and cancellation.

## Validation and limitations
`python3 -m pytest -q tests/unit/test_procs.py tests/unit/test_guardian.py
tests/unit/test_salvage.py tests/process/test_guardian_process.py`:
34 passed, 4 skipped in 1.18 s. Real process tests explicitly skip because the
host sandbox denies `ps` and `sysctl`; production inspection fails closed.
`uv` cannot resolve dependencies (no network and inherited frozen lock mode);
system pytest is the offline fallback. Push failed resolving github.com.
No v1 commands executed or files modified.
