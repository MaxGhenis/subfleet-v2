Provider attempts inherited the daemon's shared TMPDIR, so a job using `$TMPDIR/venv` collided with other jobs and cleanup could delete their files. Every detached attempt, retry, resume, conversation turn, capacity probe and supervised enrollment now gets an exclusive 0700 folder under the user's Darwin temp root, recorded before the launch gate opens. Only TMPDIR changes in the environment.

Terminal attempts reclaim scratch after verified-empty containment. Quarantine, live or uncertain census members, and salvage holds retain it. Startup and retention sweeps retry failures and age unknown store-owned allocations for 24 hours. Allocation identity and descriptor-relative deletion protect other directories and symlinks. Durable cleanup receipts pin census evidence until reclamation succeeds; remaining scratch joins retention accounting, and the existing placement reservation covers worktree and scratch growth together.

Codex's local dummy-config prompt inspection confirmed that workspace-write derives writable roots from the child TMPDIR. Writable Claude launches explicitly add the directory while preserving their permission modes. The host sandbox refused a nested Seatbelt execution probe; no real credentials or model requests were used.

Known allocations remain identifiable from durable path/device/inode/uid evidence after a provider removes the discovery marker. Unknown allocations still require a valid store-bound marker.

Validation: **312 passed, 1 skipped** in the final focused hold, including **all 58 new cases**, four named mutation witnesses, the bounded lifecycle property, and the required 38 state-contract/module-name cases. Latest per-case directory totals: `tests/unit` **10,070 passed, 9 failed, 15 skipped** across 245/331 modules (two partial); `tests/fake` **174 passed, 1 skipped** across 9/107 modules. **184 modules remain unrun** at the requested wall cutoff. The nine observed failures involve native inspection/liveness and a one-second timing assertion; the prepared base comparison was not run. Full counts, failure names, unrun files and measurement limits are in [the D-TMP1 report](docs/reports/2026-10-22-attempt-tmpdir.md).

Observed build-lane scratch reached 1.43 GB of contents / 1.56 GB by block sum; this is a sampled lower bound, with APFS sharing caveats.

Operational deviation: initial dependency setup downloaded packages before offline mode was enabled. Subsequent uv commands were offline. Shared Git metadata was unwritable, so commits are in workspace-local `.git-tmp1`; no branch was pushed.
