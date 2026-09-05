# Release gates

Plan amendment 10 and contract C-20.4. The `run` verb does not cut over from v1 to v2 until every row is green. Measurements are taken on this Mac against the fake providers unless the row says canary.

| Gate | How it is measured | Test or record | Result |
|---|---|---|---|
| Zero lost acknowledged jobs in the crash suite | `tests/fake/test_crash_matrix.py` over every boundary in C-4.2 plus export and notice | test | pending |
| Zero duplicate accepted results | the crash suite asserts one `accepted_attempt_id` per job after every recovery | test | pending |
| Zero results accepted from a stale attempt | a late `finalizing` from a superseded attempt is refused (C-4.3) | test | pending |
| Zero workspace reuse after an unverified termination | `nested-setsid` and `ignore-sigterm` scenarios end `quarantined`, never released (C-5.6) | test | pending |
| Cached `status` p95 under 100 ms | 200 calls against a store with 500 jobs and 14 lanes, `time.perf_counter` | record | pending |
| `submit` p95 under 250 ms excluding probes | 200 submits with a free lane and `probe_required` false | record | pending |
| Recovery after a daemon SIGKILL under 30 s | crash suite timing from restart to the last re-adopted or finalized attempt | record | pending |
| Guard trust preflight blocks a mismatched hash | `tests/unit/test_guard_trust.py` (C-14.2) | test | pending |
| Isolation matrix | `tests/process/test_*_isolation.py` (C-14.4) | test | pending |
| 100 representative canary jobs | one v2-owned Codex lane runs 100 real read-only jobs drawn from the v1 ledger's prompts; every job terminal with a deliverable or a classified failure; no `quarantined`, no `lost` | record | pending |
| Seven-day soak | the canary lane under v2 for seven days with the daemon's timers on; no unresolved ownership, loss, or duplicate-action defect in `events` | record | pending |
| Shadow week decision diff | nightly `compare` of v1 and v2 decisions for the same submissions; every difference explained and none worse | record | pending |
