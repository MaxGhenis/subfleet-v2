# Store and routing support progress

## State
Implementing the core lane's persistence and support modules; shared seams remain unchanged.

## Done
- Read the binding acceptance contract, shared seams, plan of record, routing example, and specified v1 reference files.
- Agreed Store and minimal policy APIs with the daemon implementer.

## Next
- Implement and test durable store transactions, identifiers, and typed persistence helpers.
- Implement policy loading and lane eligibility, credential resolution, lazy adapter registry, and retention.
- Commit coherent steps, attempt each required push, and report validation and limitations.

## Step 1 validation
+- Implemented `store.py` and `ids.py`: WAL/FULL SQLite, readonly mode, version refusal, serialized transactions and events, persistence helpers, immutable bindings, closure extension, identifiers and canonical digests.
+- `python3 -m pytest -q tests/unit/test_store_ids.py`: 9 passed in 0.07 s.
+- Required `uv run` currently reports missing lockfile with inherited `UV_FROZEN=1`; root is managing environment setup.
+- Initial push failed because `github.com` cannot resolve; no bypass attempted.

## Step 2 validation
+- Added the plan's model map and caps in `default_policy.json`, validated policy loading and exact-byte hashing, and minimal pinned/first-eligible lane selection.
+- Added environment-only credential resolution and injectable lazy provider adapter construction.
+- Added retention with active/quarantine/notice/salvage/gate/lease pins and file accounting/deletion outside transactions.
+- `python3 -m pytest -q tests/unit/test_store_ids.py tests/unit/test_policy_support.py`: 19 passed in 0.08 s.
+- Full upward chain routing, provider comparators, and preflight probing remain intentionally assigned to the later routing lane (C-11.2 to C-11.6).

## State
+Support implementation and focused verification complete; ready for daemon integration. No shared seam changes.

## Next
+- Integrate with daemon checks and address concrete failures.
+- Required pushes continue to fail DNS resolution for github.com; final report must record this limitation.

## Independent daemon review
+- C-8.1/C-8.3: reported concurrent `_export` entry from finalization and the control loop; one publisher could release the lease while another still writes the old result. Root owns the fix and regression test.
+- C-4.5: reproduced a transient retry choosing a different newly available lane, and a pinned lane waiting forever after its second transient failure. Root owns retry fixes.
+- C-6.4: noted that wall time was checked only for live attempts, leaving waiting retries outside the deadline.
+- C-8.4/C-13.4: reported that the maintenance helper removes job artifacts but does not remove allocated Git worktrees; this requires integrator follow-up or explicit scope reporting.
+- Review used deterministic daemon state with constructor identity stubs, never spawned providers, and made no daemon edits.

## Allocated worktree retention follow-up
+- Implemented the previously reported C-8.4/C-13.4 gap: byte accounting now includes allocated worktrees, and selected owned linked worktrees are removed with their Git registrations outside transactions.
+- In-place workdirs, external resolved paths, and symlinked worktree containers are never removed. Dirty worktrees require a recorded existing salvage ref matching the exact current tree before a forced removal; unlanded salvage remains pinned by default.
+- A durable `worktree:<path>` lease held by `retention:<job id>` fences new writers throughout removal and can resume after a crash between Git removal and row deletion.
+- `UV_CACHE_DIR="$PWD/.uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run pytest -q tests/unit/test_store_ids.py tests/unit/test_policy_support.py tests/unit/test_retention_worktrees.py`: 29 passed in 1.06 s.
+- All Git inspection, temporary-index preparation, byte accounting, and removal remain outside store transactions; isolated-repository tests cover clean cleanup, dirty preservation, exact salvage matching, path ownership, in-place preservation, and deletion recovery.
