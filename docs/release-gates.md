# Release gates

Plan amendment 10 and contract C-20.4. The `run` verb does not cut over from v1 to v2 until every row is green. Measurements are taken on this Mac against the fake providers unless the row says canary.

| Gate | How it is measured | Test or record | Result |
|---|---|---|---|
| Zero lost acknowledged jobs in the crash suite | `tests/fake/test_crash_matrix.py::test_c20_3_crash_matrix_recovers_without_duplicate_acceptance` over every boundary in C-4.2 plus export and notice; `tests/fake/test_state_contract.py::test_c4_3_state_acceptance_and_notice_share_one_transaction` | test | green 2026-09-05 (2780-test run outside the sandbox) |
| Zero duplicate accepted results | the crash suite asserts one `accepted_attempt_id` per job after every recovery; `tests/fake/test_state_contract.py::test_c6_2_state_concurrent_duplicate_requests_create_one_job` | test | green 2026-09-05 |
| Zero results accepted from a stale attempt | `tests/fake/test_finalization_replay.py::test_c4_3_stale_attempt_cannot_publish_or_accept`; `test_c4_2_classification_and_attestation_are_frozen_before_acceptance` (C-4.3) | test | green 2026-09-05 |
| Zero workspace reuse after an unverified termination | `tests/fake/test_daemon_contract.py::test_c5_5_nested_setsid_quarantines_and_force_release_records_override`; `tests/fake/test_state_contract.py::test_c4_2_state_unverifiable_starting_quarantines_and_keeps_workspace`; `test_c5_7_state_quarantine_confirm_dead_requires_empty_and_override_is_audited` (C-5.6, C-5.7) | test | green 2026-09-05 |
| Cached `status` p95 under 100 ms | `tools/measure_release_gates.py --jobs 300 --calls 200`: 200 CLI calls against a store with 300 terminal jobs and 14 lanes, timed end to end including Python interpreter startup | record | 106 ms on 2026-09-05 14:40 (MacBook Pro). Marginal miss; the number includes about 90 ms of interpreter and import time per CLI process, so the daemon's own cached response is well under the target. Follow-up: measure the socket round trip alone and record both. |
| `submit` p95 under 250 ms excluding probes | same run, 200 submits on measured lanes (no probe) | record | 142 ms on 2026-09-05 14:40. Green. |
| Recovery after a daemon SIGKILL under 30 s | same run: a running slow job, SIGKILL the daemon, restart, time to the job's terminal state | record | 3.2 s on 2026-09-05 14:40; one attempt, re-adopted, succeeded. Green. |
| Guard trust preflight blocks a mismatched hash | `tests/unit/test_guard_trust.py` (C-14.2) | test | pending |
| Isolation matrix | `tests/process/test_*_isolation.py` (C-14.4) | test | pending |
| 100 representative canary jobs | one v2-owned Codex lane runs 100 real read-only jobs drawn from the v1 ledger's prompts; every job terminal with a deliverable or a classified failure; no `quarantined`, no `lost` | record | pending |
| Seven-day soak | the canary lane under v2 for seven days with the daemon's timers on; no unresolved ownership, loss, or duplicate-action defect in `events` | record | pending |
| Shadow week decision diff | nightly `compare` of v1 and v2 decisions for the same submissions; every difference explained and none worse | record | pending |

## Watch list

- Two daemon tests quarantine an attempt under the full 3000-test run and pass when run alone: `tests/fake/test_daemon_contract.py::test_c5_6_c13_1_writable_kill_salvages_dirty_workspace_after_verified_containment` and the Claude workspace-write case of `tests/e2e/test_guard_and_isolation.py::test_provider_environment_isolated_in_each_sandbox`. Seen 2026-09-05 17:00 after the state-root marker fix, so the cause is not the cross-root marker collision. The kept state roots under `/tmp/sf-failed/` show a live pid outside the recorded group at kill time. Diagnose before the seven-day soak; a quarantine that a rerun does not reproduce is exactly the class of defect the soak exists to catch.
