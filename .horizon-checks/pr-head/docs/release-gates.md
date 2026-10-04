# Release gates

Plan amendment 10 and contract C-20.4 define the original release gates below.
Measurements are taken on this Mac against the fake providers unless the row
says canary. The operator's 2026-09-19 direct-cutover decision in `docs/plan.md`
overrides the staged rollout schedule for this installation; it does not turn
unperformed canary or soak observations into passes.

## Current evidence, 2026-09-19

**Direct cutover authorized; staged rollout waived.** Max explicitly requested
a clean cutover after the implementation review. The real 100-job canary,
seven-day soak, and nightly shadow comparisons have no verified completion
record and remain unperformed. Before migration, a read-only inspection found
no `state.sqlite3` or `soak.json` under `~/.subfleet`; the directory contained
login and temporary directories. Historical plans and September 5 measurements
do not establish that a live shadow week happened or certify the current checkout.

Cutover preparation backs up the command target, existing app, rosters,
launch-agent files, hook settings, and v1 run metadata. Running v1 jobs keep
their original runners and account ownership until they finish. The retained
Swift menu app now reads v2 `status.json`, marks stale/offline and identity or
ownership problems, and reloads the snapshot without invoking v1's watchdog.
Its Foundation-only tests decode actual Python-generated status projections;
the full app is compiled and signed without launching it during tests.

Migration regressions cover canonical reset-credit operation keys, recognition
of legacy imported keys, and refusal to transfer busy Claude or Codex accounts.
Initial migration keeps automatic redemption disabled until imported action
history and sole ownership have been verified. Operational backup and cutover
records are kept under `~/.subfleet/cutovers/`.

The release tools now fail incomplete or unverifiable evidence: `canary_check.py` returns nonzero for `PENDING`, verifies deliverable bytes against their stored size and SHA-256, and requires a matching accepted attempt, the complete isolation configuration, and a verified unchanged clone. Each job must record isolated read-only execution, with its workdir and review root resolving to that clone; every recorded launch must use the same cwd without a directory override. The submitter freezes every sampled prompt and its SHA-256 before committing the first baseline and submitting any job. Reruns verify and reuse those exact bytes, even if the source ledger changes, preserve the original baseline, and fail rejected submissions. Transfer verification requires the relocated credential, a readable v1 status, and a matching v2 owner/home; a rerun preserves the soak start.

`soak_report.py` defaults to the previous complete UTC day. A clean observation requires a recorded UTC start and a successful scheduled probe cycle. Losses and quarantines remain blockers across day boundaries, including quarantines without a finish timestamp. Identity mismatches and unknown or stuck actions also block. Imported history is identified by JSON value, independent of whitespace. A clean report is one observation, **not proof of seven continuous clean days**. Ownership continuity, explained anomalies, coverage of the complete seven-day window, and shadow decision differences still need operator review.

`measure_release_gates.py` checks every measured CLI exit status, uses a distinct verified fixture identity for each Claude lane, and returns nonzero when any latency threshold fails or SIGKILL recovery does not end with one succeeded attempt. Socket latency is diagnostic; it cannot substitute for the end-to-end CLI status threshold.

The numeric gates were remeasured on this Mac on 2026-09-19 at 08:26 EDT with `uv run python tools/measure_release_gates.py --jobs 300 --calls 200`: **PASS, exit 0**. End-to-end status p95 was 83 ms, submit p95 was 119 ms, and SIGKILL recovery took 3.4 s with one succeeded attempt. [Recorded measurements](reports/2026-09-19-release-measurements.md) supersede the September 5 numbers for these gates only.

Regression verification: `uv run pytest -q tests/unit/test_soak_tools.py` — 49 passed (2026-09-19); `sh -n tools/canary_runbook.sh` passed. All fixtures use temporary state roots, fake providers, and temporary git repositories. No account transfer, provider call, installed-daemon change, or CLI cutover was performed for these checks.

## Measurements and outstanding operational gates

| Gate | How it is measured | Test or record | Result |
|---|---|---|---|
| Zero lost acknowledged jobs in the crash suite | `tests/fake/test_crash_matrix.py::test_c20_3_crash_matrix_recovers_without_duplicate_acceptance` over every boundary in C-4.2 plus export and notice; `tests/fake/test_state_contract.py::test_c4_3_state_acceptance_and_notice_share_one_transaction` | test | green 2026-09-05 (2780-test run outside the sandbox) |
| Zero duplicate accepted results | the crash suite asserts one `accepted_attempt_id` per job after every recovery; `tests/fake/test_state_contract.py::test_c6_2_state_concurrent_duplicate_requests_create_one_job` | test | green 2026-09-05 |
| Zero results accepted from a stale attempt | `tests/fake/test_finalization_replay.py::test_c4_3_stale_attempt_cannot_publish_or_accept`; `test_c4_2_classification_and_attestation_are_frozen_before_acceptance` (C-4.3) | test | green 2026-09-05 |
| Zero workspace reuse after an unverified termination | `tests/fake/test_daemon_contract.py::test_c5_5_nested_setsid_quarantines_and_force_release_records_override`; `tests/fake/test_state_contract.py::test_c4_2_state_unverifiable_starting_quarantines_and_keeps_workspace`; `test_c5_7_state_quarantine_confirm_dead_requires_empty_and_override_is_audited` (C-5.6, C-5.7) | test | green 2026-09-05 |
| Cached `status` p95 under 100 ms | `tools/measure_release_gates.py --jobs 300 --calls 200`: 200 `status` calls against a store with 300 terminal jobs and 14 lanes, measured two ways: the CLI end to end (a fresh Python process per call) and the socket round trip from one in-process client (the three requests `subfleet status` makes: `daemon.status`, `lanes`, `readings`) | record | Green 2026-09-19 08:26 EDT: CLI 83 ms p95; socket 24 ms p95; interpreter start plus CLI import 59 ms p95 (diagnostic). Supersedes September 5 measurements. |
| `submit` p95 under 250 ms excluding probes | same run, 200 submits on measured lanes (no probe) | record | Green 2026-09-19 08:26 EDT: 119 ms p95. Supersedes September 5 measurements. |
| Recovery after a daemon SIGKILL under 30 s | same run: a running slow job, SIGKILL the daemon, restart, time to the job's terminal state | record | Green 2026-09-19 08:26 EDT: 3.4 s; one attempt, re-adopted, succeeded. Supersedes September 5 measurements. |
| Guard trust preflight blocks a mismatched hash | `tests/unit/test_guard_trust.py` (C-14.2) | test | green 2026-09-05 (part of an 84-test run with the isolation matrix, 10 s) |
| Isolation matrix | `tests/process/test_claude_isolation.py`, `tests/process/test_codex_isolation.py`, `tests/e2e/test_guard_and_isolation.py` (C-14.4) | test | green 2026-09-05; the Claude workspace-write case flakes under full-suite load, see the watch list |
| 100 representative canary jobs | one v2-owned Codex lane runs 100 real read-only jobs drawn from the v1 ledger's prompts; every job terminal with a deliverable or a classified failure; no `quarantined`, no `lost` | record | pending |
| Seven-day soak | the canary lane under v2 for seven days with the daemon's timers on; no unresolved ownership, loss, or duplicate-action defect in `events` | record | pending |
| Shadow week decision diff | nightly `compare` of v1 and v2 decisions for the same submissions; every difference explained and none worse | record | pending |

## Watch list

- **Load-dependent false quarantines and false losses, 2026-09-05.** Two daemon tests quarantined an attempt under the full suite at a load average of 20 to 25 and passed alone (`tests/fake/test_daemon_contract.py::test_c5_6_c13_1_writable_kill_salvages_dirty_workspace_after_verified_containment`; the Claude workspace-write case of `tests/e2e/test_guard_and_isolation.py::test_provider_environment_isolated_in_each_sandbox`). Neither kept root survived. Re-running the suite under two parallel daemon-suite load generators (load average 45 to 75) did not reproduce the quarantines but produced a worse defect with roots kept: jobs whose provider had exited 0, with `exit.json` on disk, recorded `lost` with "guardian lost without exit receipt". Cause: the tick read the receipt path before the guardian published it, then found the guardian gone (it exits right after writing), and finalized with the stale `lost` verdict. Fixes, all in `lane/quarantine-flakes`: three-valued guardian liveness so an inspection failure decides nothing (C-4.2); a second receipt read before a dead guardian becomes a loss; the receipt on disk wins at finalization; one process-table snapshot for the census (C-5.5); bounded settle windows after SIGKILL and after the exit receipt before quarantining (C-5.6, C-5.9). The quarantines are the same family (a single process-table read racing the kernel's teardown under load); the settle windows remove the single-read decision. The soak still has to confirm: any `quarantined` or `lost` attempt whose kept evidence shows a receipt or an empty census on re-read is a regression of this item.
