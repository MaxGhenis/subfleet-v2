# D-REVIEW-FAMILY1: independent reviewer routing

Implemented the routing, authorization, gate, migration and Codex writer changes. Final selected checks: **2,464 passed, 7 skipped, 1 existing expected failure across 60 files**, including **91 tests in seven new files**. Git could not commit because its external index lock is outside sandbox permissions.

Started 2026-11-15 08:03:20 UTC, from the first command `date -u`; deadline 12:03:20 UTC. Workspace began clean at detached commit `5b06c1826bc0`. Work stays in the assigned checkout.

## Behavior

`subfleet/policy.py:routing_chain` filters a review chain from the requested tier upward to providers other than the recorded caller family. If none remain, `REVIEW_FAMILY_DEFAULTS` supplies `sol61` for Claude callers and `opus` for Codex callers. Optional `review_family_defaults` overrides either entry; a policy without the key uses the code defaults and is never rewritten. Validation requires an existing model from the other family.

The numbered routing rule controls: Astra remains the Claude caller's eligible model in the shipped chain; Sol is the default when the eligible suffix has no Codex model. This resolves the brief's introductory “Sol first” shorthand in favor of its explicit candidate-order rule.

`scheduler.py` shares the chain across evaluation, provider pinning, and demand/FIFO accounting; `daemon.py` uses it during submission validation, and `route_check.py` preserves its wait reason during reservation rechecks. No capacity in the required family means waiting and a reason naming that family. Existing same-family pins are also guarded at evaluation. Explicit same-family model submissions refuse with `review-same-family`, exit 7, before job/event insertion. `--allow-same-family` records a boolean on the job, authorizes a same-family pin, and appends the original chain after independent choices. True authorization joins the request digest; default false leaves old digests intact.

`why --caller-family` overrides normal caller detection; shape explanations otherwise detect exactly as `run` does. Existing-job explanations use the job's family. `runs show --json` carries `caller_family` and `allow_same_family`. CLI help, README, and acceptance clauses C-6.23–24 document the behavior, with C-3.8/C-6.5/C-6.21/C-17.2 corrected and one contiguous change-log entry added.

## What `why` prints

A synthetic no-lane snapshot using the shipped policy produced these exact `Walk:` suffixes. This exercises the same evaluator and renderer used by `why`; CLI tests separately prove detection and explicit-flag transport. When a lane can start, the walk stops at the chosen model as before.

| Tier | Claude caller | Codex caller | Null caller |
| --- | --- | --- | --- |
| trivial | `astra` | `haiku -> sonnet -> opus` | `haiku -> sonnet -> opus -> astra` |
| easy | `astra` | `sonnet -> opus` | `sonnet -> opus -> astra` |
| standard | `astra` | `opus` | `opus -> astra` |
| hard | `astra` | `opus` | `astra` |

Known Claude callers also see `waiting for other-family reviewer: missing codex capacity`; known Codex callers see `waiting for other-family reviewer: missing claude capacity`. Null callers retain the old reason. With no Codex model in the eligible suffix, the Claude caller's walk is `sol61`; I5 checks this for every tier with the new policy key absent and present.

## Gate and Codex writers

The gate now records detected caller family, treats it as authoritative for the main author, and rejects a peer from the same family even without `--main-model`. Explicit main-model identity must agree. Gate reviewer jobs retain the family. Null identity keeps complementary-family attestation.

Static repository inspection found `CODEX_THREAD_ID` and `CODEX_SESSION_ID`, but no established Codex PID marker. The process table records numeric ancestry and birth identity without executable-name evidence binding a thread to a particular CLI instance. No live process or environment values were inspected. Known Codex callers therefore skip only the session writer instance comparison. Worktree conflicts, revive conflicts, session caps, and SQL race rechecks remain. Tests prove a second overlapping writer on distinct targets succeeds, while shared-target conflicts and explicit caps still refuse; Claude callers still require identifiable process instances. This does not distinguish separate Codex CLI processes claiming the same thread; target exclusivity and configured caps remain the available controls.

## Migration

A2's review metadata migration is now 12. Slot 11 carries #307’s schema compatibility: five notice columns, the global caller-wake session table, and its partial notice index. Caller-wake runtime behavior is not imported. Pending/offered notices are backfilled stale only when the wake-state column is newly added; already migrated notice history is preserved. Migration 12 adds the review fields and `allow_same_family INTEGER NOT NULL DEFAULT 0 CHECK (...)`. Older read-only job readers supply false. The I6 fixture freezes #307’s actual migration DDL/backfill from local object `2b84500ab`. Both schema-10 directly upgraded through 12 and schema-10 → #307 schema-11 → 12 must reach equivalent full SQLite schema definitions, preserving original job data and existing notice/wake history. The only overlap with #307 is its migration/schema compatibility; notice delivery runtime remains unchanged.

## Invariants and mutants

| Invariant | Test and evidence |
| --- | --- |
| I1: other-family selection at every tier | `test_review_family_routing.py::test_i1_every_tier_family_and_chain_chooses_the_other_provider`: 150 generated examples plus explicit all-Claude/all-Codex chains, testing choice, chain, demand and pin provider. `test_i1_repurposed_default_name_never_selects_the_callers_provider` additionally refuses a default nickname remapped to the caller's provider. Submission/legacy-pin tests cover explicit and stale pins. |
| I2: wait without the other family; consent permits fallback | `test_review_family_routing.py::test_i2_missing_other_family_waits_and_explicit_permission_allows_fallback`: both families × four tiers. `test_review_family_admission.py::test_i2_admission_waits_without_other_family_and_override_permits_same_family`: real waiting jobs, no attempts on forbidden lanes, then authorized placement. |
| I3: null-family routing unchanged | `test_review_family_routing.py::test_i3_null_family_full_decisions_match_a2`: 150 generated comparisons. `test_review_family_admission.py::test_i3_i4_durable_decision_rows_and_events_match_a2`: full persisted decision rows and job events on open/closed fleets. |
| I4: other tasks unchanged | `test_review_family_routing.py::test_i4_other_tasks_full_decisions_match_a2_whatever_the_family`: 150 generated comparisons including null/Claude/Codex and both flag values; `test_i4_build_keeps_a2_chain_for_every_tier` adds deterministic build cases. The durable admission differential also covers builds from all families. |
| I5: optional policy key | `test_review_family_routing.py::test_i5_absent_policy_key_uses_code_default_and_explicit_key_overrides`: both families, all tiers, omitted key retains file bytes and uses code defaults; explicit values win. Six invalid-policy cases reject invalid shapes, names and same-family defaults. |
| I6: migration paths agree | `test_store_review_family_migration.py::test_i6_schema_10_and_actual_pr307_11_reach_identical_schema_and_keep_existing_rows`: actual frozen #307 DDL/backfill, complete `sqlite_master` comparison, preserved old columns/data and matching notices. Separate cases preserve schema-11 pending wake/history and prevent stale backfill on replay. |

The A2 oracle freezes five changed scheduler functions from commit `5b06c1826bc0`. An independent extraction verified all five against that Git object; fixture SHA-256 is `7ec0a2624af36014eba175e5b82a3e628b07a46630572683dd050a8dc1bac379`. New tests also cover flag persistence in the manifest/list/show, refusal without inserted jobs/events, retired model aliases, correct account/lane pins, gate identity, and Codex writer limits.

Each mutant was applied alone in a scratch copy, tested, restored from pristine bytes in `finally`, and checked by byte equality and SHA-256. All four were repeated after the default-provider safety fix; final pristine policy SHA-256 was `77fd1033df4c7c05a7aa1d89614d4b0c836d4b0b2487d107297e2268e11eaa52`.

| Mutant | Final killer in `tests/unit/test_review_family_routing.py` | Result |
| --- | --- | --- |
| Keep same-family models in the filter | `test_i1_every_tier_family_and_chain_chooses_the_other_provider` | exit 1: 1 failed, 24 deselected |
| Fall back to the same family without consent | `test_i2_missing_other_family_waits_and_explicit_permission_allows_fallback` | exit 1: 7 failed, 1 passed, 17 deselected |
| Apply family choice to builds | `test_i4_build_keeps_a2_chain_for_every_tier` | exit 1: 2 failed, 23 deselected |
| Ignore policy overrides | `test_i5_absent_policy_key_uses_code_default_and_explicit_key_overrides` | exit 1: 2 failed, 23 deselected |

The first mutant round also killed all four: respectively 1 failed/20 deselected; 7 failed/1 passed/13 deselected; 1 failed/20 deselected using the generated I4 differential; and 2 failed/19 deselected. No mutant remains in the checkout or either restored copy.

## Commands and verification

Scratch is `/private/tmp/sf-rv-a3`; pytest basetemps and automatic fixture homes are under the permitted attempt directory's `sf-rv-a3` child. Tests set `TMPDIR` from `getconf DARWIN_USER_TEMP_DIR`, `UV_CACHE_DIR` under scratch, and use the synchronized development environment. A scratch runner gives each file a new basetemp, redirects explicit `/tmp` fixture folders into scratch, checks every recursive deletion contains `sf-rv-a3`, disables checkout bytecode/cache artifacts, and records per-file results. Files run individually, with no xdist workers.

Every file was invoked individually. `T FILE` below means the actual command:

```sh
TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" \
UV_PROJECT_ENVIRONMENT=/private/tmp/sf-rv-a3/venv \
UV_CACHE_DIR=/private/tmp/sf-rv-a3/uv-cache \
PYTHONDONTWRITEBYTECODE=1 \
uv run --no-sync --cache-dir /private/tmp/sf-rv-a3/uv-cache \
  python /private/tmp/sf-rv-a3/check.py FILE
```

The scratch runner calls `pytest -q -p no:cacheprovider --basetemp=<permitted-attempt>/sf-rv-a3/pytest-<unique-nanoseconds> FILE`. Mutants add `-k KILLER` in their own source copy. P = passed, F = failed, S = skipped, X = expected failure, E = collection error. The table lists **every guarded invocation** in per-selection order, including intermediate failures and repeated final checks. Together with the eight mutant invocations above, there were **134 guarded pytest invocations**. Latest successful results are counted once per file; the final host-push selection contributes only its one migration test, so its earlier broad run is not double-counted.

| File or selection passed to `T` | Every invocation, in order (`counts / exit`) |
| --- | --- |
| `tests/fake/test_admission_latency.py` | 14P / 0; 14P / 0 |
| `tests/fake/test_admission_liveness.py` | 32P / 0; 32P / 0 |
| `tests/fake/test_admission_properties.py` | 27P+1X / 0; 27P+1X / 0 |
| `tests/fake/test_admission_route_isolation.py` | 93P / 0; 93P / 0 |
| `tests/fake/test_admission_stateful.py` | 17P / 0; 17P / 0 |
| `tests/fake/test_admission_visibility.py` | 43P / 0; 43P / 0 |
| `tests/fake/test_capacity_wait_backoff.py` | 30P / 0; 30P / 0 |
| `tests/fake/test_codex_writers.py` | 4P / 0; 4P / 0; 4P / 0 |
| `tests/fake/test_probe_cancel.py` | 23P+1F / 1; 24P / 0; 24P / 0 |
| `tests/fake/test_reset_credit_incident.py` | 19P / 0; 19P / 0 |
| `tests/fake/test_review_family_admission.py` | 2P+10F / 1; 10P+2F / 1; 12P / 0; 12P / 0 |
| `tests/fake/test_review_family_submission.py` | 14P / 0; 15P / 0; 16P / 0; 16P / 0 |
| `tests/fake/test_review_subject_submission.py` | 12P / 0; 12P / 0; 12P / 0 |
| `tests/fake/test_review_subject_validation.py` | 29P+1F / 1; 30P / 0; 30P / 0 |
| `tests/fake/test_state_contract.py` | 37P / 0; 37P / 0 |
| `tests/fake/test_status_payload.py` | 2P / 0; 2P / 0 |
| `tests/fake/test_task_hold_admission.py` | 49P / 0; 49P / 0 |
| `tests/fake/test_unadmittable_pin_review.py` | 7P / 0; 7P / 0 |
| `tests/fake/test_unrun_worktrees.py` | 98P / 0; 98P / 0 |
| `tests/fake/test_writable_fanout.py` | 24P+4F / 1; 28P / 0 |
| `tests/unit/test_cli.py` | 256P / 0; 256P / 0 |
| `tests/unit/test_cli_batch.py` | 18P / 0; 18P / 0 |
| `tests/unit/test_cli_turns.py` | 23P / 0; 23P / 0 |
| `tests/unit/test_conversation_status.py` | 9P / 0 |
| `tests/unit/test_conversation_store.py` | 32P / 0 |
| `tests/unit/test_daemon_verbs.py` | 15P+5S / 0; 15P+5S / 0 |
| `tests/unit/test_gate_admission.py` | 24P / 0; 24P / 0; 24P / 0 |
| `tests/unit/test_gate_review_family.py` | 8P+2F / 1; 10P / 0; 10P / 0; 13P / 0; 13P / 0 |
| `tests/unit/test_gate_service.py` | 25P / 0; 25P / 0 |
| `tests/unit/test_gate_verdict.py` | 45P / 0 |
| `tests/unit/test_handler_refusals.py` | 386P / 0; 386P / 0 |
| `tests/unit/test_host_push.py` | 120P / 0 |
| `tests/unit/test_host_push.py::test_schema_six_migration_preserves_jobs_without_authorizing_push` | 1P / 0; 1P / 0 |
| `tests/unit/test_ids.py` | 0 tests / 4 |
| `tests/unit/test_importer.py` | 149P+2S / 0 |
| `tests/unit/test_invariants_index.py` | 25P / 0; 25P / 0 |
| `tests/unit/test_offline.py` | 33P / 0; 33P / 0 |
| `tests/unit/test_policy.py` | 239P / 0 |
| `tests/unit/test_policy_tier_preferences.py` | 15P / 0 |
| `tests/unit/test_protocol.py` | 0 tests / 4 |
| `tests/unit/test_protocol_registry.py` | 4P / 0; 4P / 0 |
| `tests/unit/test_prune_217.py` | 32P / 0; 36P+1F / 1; 37P / 0 |
| `tests/unit/test_prune_decisions.py` | 48P / 0; 48P / 0 |
| `tests/unit/test_qos_review_r2.py` | 5P / 0 |
| `tests/unit/test_reserve_authorization_plumbing.py` | 5P / 0; 5P / 0 |
| `tests/unit/test_retention_archive.py` | 109P / 0 |
| `tests/unit/test_retention_review_subject.py` | 10P / 0; 10P / 0 |
| `tests/unit/test_review_family_cli.py` | 11P / 0; 11P / 0; 11P / 0; 11P / 0 |
| `tests/unit/test_review_family_routing.py` | 20P+1F / 1; 21P / 0; 25P / 0 |
| `tests/unit/test_review_r6_probes.py` | 30P / 0 |
| `tests/unit/test_review_subject_cli.py` | 54P / 0; 54P / 0 |
| `tests/unit/test_route_check.py` | 24P / 0 |
| `tests/unit/test_scheduler_tier_preferences.py` | 9P / 0 |
| `tests/unit/test_store_ids.py` | 27P / 0; 27P / 0 |
| `tests/unit/test_store_migration_2.py` | 21P / 0; 21P / 0 |
| `tests/unit/test_store_qos_migration.py` | 13P / 0; 13P / 0 |
| `tests/unit/test_store_review_family_migration.py` | 9P / 0; 10P / 0 |
| `tests/unit/test_store_review_subject_migration.py` | 11P / 0; 11P / 0 |
| `tests/unit/test_suggestions.py` | 30P / 0 |
| `tests/unit/test_suggestions_r1b_adversarial.py` | 30P / 0 |
| `tests/unit/test_test_module_names.py` | 1P / 0; 1P / 0 |
| `tests/unit/test_timers_daemon.py` | 4P / 0; 4P / 0 |
| `tests/unit/test_wait_hub.py` | 44P / 0; 44P / 0 |

Before the guarded runner, these additional commands ran with `TMPDIR` from `getconf`, scratch `UV_CACHE_DIR`, `uv run --no-sync pytest -p no:cacheprovider --basetemp=/private/tmp/sf-rv-a3/<unique>`: new migration file RED 9F/exit1, then GREEN 9P/exit0; A2 migration 11P/exit0; numbered migrations 21P/exit0; QoS migration 13P/exit0; store IDs 0 tests/1 collection error/exit2 (the moved environment caused fallback Python to lack Hypothesis); reserve plumbing 4P+1F/exit1. All were rerun correctly; the final actual-#307 migration suite grew to 10 tests.

Intermediate failures were resolved without weakening checks: a gate fixture compared setup events to an empty list; the routing property needed reserve-independent capacity; a reservation recheck referred to an undefined local; admission fixtures needed fresh readings to avoid the normal probe path; an A2 metadata fixture pinned same-family Astra; a partial-index fixture dropped its referenced column first; and an explicit top-level TMPDIR fixture was sandbox-denied. The two optional nonexistent paths `tests/unit/test_ids.py` and `tests/unit/test_protocol.py` each exited4 with no tests. Existing store-ID, protocol registry, CLI, and digest/submission coverage passed.

The seven final skips are five `test_daemon_verbs.py` cases and two `test_importer.py` cases requiring unavailable process inspection. The existing strict xfail is `test_c6_10_a_wait_of_another_kind_does_not_record_the_same_verdict_again` in `test_admission_properties.py`; its assertion was unchanged. No required existing file or new test file remains unrun, and no functional failure remains unresolved.

Other commands and results (read/edit batches grouped by purpose):

| Commands | Result/counts |
| --- | --- |
| First `date -u`; `pwd`; `git status --short`; `git branch --show-current` | exit0; 08:03:20 UTC; assigned workspace; zero initial changes; detached checkout. |
| `cat /Users/maxghenis/reviews/subfleet-hub/idle-admission-2026-11-07/brief-build.md` | exit0; Rules read by all workers. |
| `rg --files` inventories; `rg -n` caller identity, chains, migrations, gate, refusal and CLI references; bounded `sed`/`cat` reads in `subfleet/`, `tests/`, README and contract | successful inspections; missing optional `test_gate_cli.py`, `test_lane_pins.py`, `test_ids.py` and `test_protocol.py` paths/no-match searches returned1/2 as applicable, no files changed. No ancestor/home directory search. |
| Exact-compare searches for job rows/fields and `schema_version`/`SCHEMA_VERSION`, plus notice-row compares after schema11 compatibility | all identified runnable comparison files exercised above; fixture-only modules are support code, not standalone tests. Broad preliminary search recorded276 matching lines, then narrowed to relevant comparisons. |
| Initial `mkdir`/`uv sync --group dev` at default Darwin temp cache | exit2; sandbox denied top-level directory/cache creation. |
| `mkdir -p /private/tmp/sf-rv-a3`; `uv sync --group dev --cache-dir /private/tmp/sf-rv-a3/uv-cache` | exit0; Python3.15.2; 10 packages resolved, 9 installed. |
| Move `.venv` into scratch; repeat sync with `UV_PROJECT_ENVIRONMENT=/private/tmp/sf-rv-a3/venv` | exit0; 9 packages checked; subsequent commands use that environment. |
| `git log`/`git show` on local commits `5b06c1826bc0`, `37d96d313`, `85769b826`, `2b84500ab`; AST extraction and SHA checks | exit0; five A2 functions verified verbatim; actual #307 migration frozen independently. |
| Python/apply-patch source, test and documentation edits; scratch runner, copy, mutant and sequential-driver scripts | exit0; source only in assigned checkout, temporary work only under scratch. |
| Synthetic evaluator/renderer matrix with shipped policy | exit0; all12 caller/tier walks shown above; no live daemon/provider contact. |
| Byte/SHA checks after each mutant and after both rounds | exit0; all8 restorations exact; original checkout policy unchanged by mutation work. |
| Repeated `git diff --check`, `git diff --stat`, `git status --short` | exit0; zero whitespace errors; only named implementation, test, fixture and documentation changes. |
| Named-file `git add` and `git commit` for routing, migration, gate/writer, submission/docs and final report steps | each attempted mutation exit128; common-repository `index.lock` denied; zero commits. |
| Guarded cleanup and final `date -u`/status check | final paths and finish time recorded below. |


## Commit and cleanup status

Named-file `git add` and `git commit` attempts were denied: creating the common repository's worktree `index.lock` is outside sandbox write permissions. Project history was not rewritten or published; the local test-push deviation is recorded below. The brief explicitly permits preserving files when this happens; changes remain for Subfleet salvage.

`uv sync` initially created checkout `.venv`; it was moved into scratch. One already-running agent command recreated it transiently; that copy was moved to `/private/tmp/sf-rv-a3/accidental-recreated-venv` and deleted using guarded `shutil.rmtree`. Final cleanup moved the following generated cache directories into `/private/tmp/sf-rv-a3/checkout-cleanup/` and deleted them with guarded `shutil.rmtree`:

- `/Users/maxghenis/.subfleet/worktrees/20261113-132326-rv-a3-family-build/subfleet/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-132326-rv-a3-family-build/subfleet/adapters/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-132326-rv-a3-family-build/subfleet/conversations/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-132326-rv-a3-family-build/subfleet/sessions/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-132326-rv-a3-family-build/tests/__pycache__`
- `/Users/maxghenis/.subfleet/worktrees/20261113-132326-rv-a3-family-build/tests/unit/__pycache__`

Both external scratch trees, `/private/tmp/sf-rv-a3` and the permitted attempt directory’s `sf-rv-a3` child, are also removed. Every recursive deletion asserts `sf-rv-a3` occurs in the target path; read-only fixture permissions are repaired only inside those paths. No scratch folder, log, cache, or dependency directory remains in the checkout. All test drivers and workers have finished.

## Deviations and limits

The complete schema-compare file `tests/unit/test_host_push.py` was run once before inspection revealed that its fixtures execute actual Git pushes to temporary local bare repositories. That did not follow the command-as-data constraint. No live remote or project ref was touched. Later runs select only `test_schema_six_migration_preserves_jobs_without_authorizing_push`; new guards and gate tests pass commands as Python data. This is an execution deviation, not a remaining routing defect.

An initial top-level Darwin temp/cache directory was denied. Before the guarded runner was installed, migration checks used ordinary pytest fixture cleanup and transient checkout bytecode/dependency folders; all identified checkout folders are listed in the cleanup record. The final checks use guarded scratch paths. The complete repository suite and live-host tests were intentionally not run, per the brief. Process-inspection-dependent skips and the existing expected failure are listed with final test counts.

Finished 2026-11-15 08:45:52 UTC; within the four-hour limit. Final cleanup exited0; six generated checkout cache directories and both scratch trees were removed. Final project status contains only the 32 intended changed/new files.
