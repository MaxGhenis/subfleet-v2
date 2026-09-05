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
