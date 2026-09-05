# Process and salvage progress

## State
Process ownership, guardian publication and temporary-index salvage implemented
and tested. Available for daemon integration.

## Done
- Read C-4, C-5, C-8 and C-13 and shared seams, plans and specified v1 behavior.
- Fixed the public API with the daemon implementer without changing shared seams.
- Implemented recorded boot/start identities, guarded signals, and three-source
  containment with identities only in evidence (no environment snapshots).
- Implemented detached guardian, start/exit receipts, commit-before-launch pipe
  gate, and temp/file-fsync/rename/directory-fsync publication.
- Tested identity mismatches, failed census sources, zombies, signal guards,
  environment-only credentials, receipt order, gate EOF and ENOSPC publication.
- Implemented writable branch refusal and private salvage refs rooted at the
  reserved baseline commit, comparing tree hashes even after provider commits.
- Proved salvage leaves HEAD, the real index, tracked/untracked/ignored files
  unchanged and replay preserves old snapshots.
- Reviewed daemon launch gating, containment recovery and quarantine resolution;
  sent concrete follow-ups to the daemon implementer without changing daemon.py.

## Next
- Integrate with daemon recovery and cancellation.
- Re-run real process acceptance on a host that permits `ps` and `sysctl`.

## Validation and limitations
`UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv sync --group dev`
succeeded after the root lane recovered cached dependencies offline.

`UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run pytest -q
tests/unit/test_procs.py tests/unit/test_guardian.py
tests/unit/test_salvage.py tests/process/test_guardian_process.py`:
34 passed, 4 skipped in 1.16 s. Real process tests explicitly skip because the
host sandbox denies `ps` and `sysctl`; production inspection fails closed.
Pushes fail resolving github.com, including both implementation commits.
No v1 commands executed or files modified.
