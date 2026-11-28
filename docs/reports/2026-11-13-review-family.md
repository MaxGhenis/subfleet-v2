# D-REVIEW-FAMILY1: independent reviewer routing

Implemented the routing, authorization, gate, migration and Codex writer changes. Final selected checks: **2,464 passed, 7 skipped, 1 existing expected failure across 60 files**, including **91 tests in seven new files**. Git could not commit because its external index lock is outside sandbox permissions.

Started 2026-11-15 08:03:20 UTC, from the first command `date -u`; deadline 12:03:20 UTC. Workspace began clean at detached commit `5b06c1826bc0`. Work stays in the assigned checkout.

## Behavior

`subfleet/policy.py:routing_chain` filters a review chain from the requested tier upward to providers other than the recorded caller family. If none remain, `REVIEW_FAMILY_DEFAULTS` supplies `sol61` for Claude callers and `opus` for Codex callers. Optional `review_family_defaults` overrides either entry; a policy without the key uses the code defaults and is never rewritten. Validation requires an existing model from the other family.

The numbered routing rule controls: Astra remains the Claude caller's eligible model in the shipped chain; Sol is the default when the eligible suffix has no Codex model. This resolves the brief's introductory “Sol first” shorthand in favor of its explicit candidate-order rule.

`scheduler.py` shares the chain across evaluation, provider pinning, and demand/FIFO accounting; `daemon.py` uses it during submission validation, and `route_check.py` preserves its wait reason during reservation rechecks. No capacity in the required family means waiting and a reason naming that family. Existing same-family pins are also guarded at evaluation. Explicit same-family model submissions refuse with `review-same-family`, exit 7, before job/event insertion. `--allow-same-family` records a boolean on the job, authorizes a same-family pin, and appends the original chain after independent choices. True authorization joins the request digest; default false leaves old digests intact.

`why --caller-family` overrides normal caller detection; shape explanations otherwise detect exactly as `run` does. Existing-job explanations use the job's family. `runs show --json` carries `caller_family` and `allow_same_family`. CLI help, README, and acceptance clauses C-6.23–24 document the behavior, with C-3.9/C-6.5/C-6.21/C-17.2 corrected and one contiguous change-log entry added.

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

## Fix round 1

Started 2026-11-17 04:10:11 UTC (first command, `date -u`); deadline 07:10:11 UTC. The checkout began clean at detached `48dab4abe`. Every finding in `review-a3-r1.md` is fixed: P1, all four P2s and both P3s. None is left open. Git wrote its lock files this round, so each step is a commit.

| Commit | Step |
| --- | --- |
| `127576d13` | Merge of #314's head `2954abd77` (D-SCHEMA-SLOTS1) |
| `b8d3304ef` | Fix code for every finding, the new tests and the fixtures |
| `a391f84e1` | Test fixes: the unit conftest drops a job's markers; a CLI fake daemon advertises `review.subject.v1` |
| `9916047b1` | Contract, README and `run --help` |
| `30ebc817c`, then this commit | This section, then its cleanup record |

### Merge of 2954abd77

`git merge --no-ff --no-commit 2954abd77` conflicted in nine files. Each was resolved as follows:

- `subfleet/store.py`: slots 11 and 12 are now exactly #314's. Slot 11 is #307's storage schema; slot 12 holds the review fields. Each step is presence-checked, and versions are recorded only after the schema script succeeds, all in one transaction. This PR's `allow_same_family` column moves to a new slot 13 (`REVIEW_FAMILY_MIGRATION`, presence-checked the same way), and `SCHEMA_VERSION` becomes 13. The presence pass now covers slots 11 to 13 on every writable open.
  - I dropped this PR's slot-11 backfill, which marked pending notices `stale`/`before-migration`. #314's slot 11 has none, and the ruling says slot 11 is exactly #314's.
  - `NOTICE_WAKE_PREDATES` is gone.
  - As a result, a store upgraded straight from schema 10 keeps its pending notices with an empty wake state. A store that ran #307 keeps #307's own stale marks. The link-7 merge brought the backfill back (see [Link-7 merge and the migration test](#link-7-merge-and-the-migration-test-670653bf9-e0bb74d2f-4d52eca3e)).
  - If #307 merges later onto these slots, it must do its backfill in a slot of its own.
- `subfleet/store_schema.sql`: took #314's comment and set the header to version 13.
- `subfleet/daemon.py` (writer check): took #314's comment. The P2 fix below then replaced the check itself.
- `subfleet/cli.py` (`caller_pid`): took #314's keyword `family` signature and kept this PR's docstring.
- `tests/unit/test_prune_217.py`, `tests/unit/test_store_migration_2.py`: kept this PR's index-dependency check, which is a superset of #314's: it reads index columns and the partial-index predicate, so automatic indexes are covered. `CREATE TABLE IF NOT EXISTS` statements drop with `IF EXISTS`. The auto-merge had imported `re` twice; one import was removed.
- `tests/unit/test_store_review_subject_migration.py`: #314's version, with the version-13 step added to the expected versions and events.
- `README.md`: kept both sides' rows and sentences.
- `docs/acceptance-contract.md`, in four places:
  - the change log keeps both lines;
  - C-3.8 is #314's clause plus the slot-13 sentence;
  - C-6.5 is #314's clause plus this PR's refusal;
  - C-6.21 and C-6.22 are #314's, and C-6.23 and C-6.24 are this PR's.

Two files merged cleanly but needed edits:
- In `tests/unit/test_store_upgrade_slots.py`, the version asserts now read `SCHEMA_VERSION`. The recorded-version test runs for 12 and 13: a store recorded at 12 gains exactly version 13 and its two audit events, and a store recorded at 13 gains nothing.
- `tests/unit/test_store_review_family_migration.py` now expects no backfill and versions `[11, 12, 13]`.

### P1: a store that ran #314's code could not be opened

- **Cause:** #314 and #330 both used migration slot 11, for different columns.
- **Change:** the merge above.
- **Evidence:** `tests/unit/test_store_upgrade_slots.py` now runs over six store layouts. Each layout was dumped from that revision's own, unmodified `Store`, run from `git archive <commit> subfleet` in scratch (`make_fixture.py`, which also adds one synthetic job and one notice). The three new ones are:
  - `review-subject-new-v12.sql`: #314's new 11 + 12 layout, made by `2954abd77`.
  - `review-family-old-v12.sql`: a store made by this PR's first head, `48dab4abe`.
  - `review-family-old-over-subject-old-v12.sql`: the review's P1 case, made with real code. `48dab4abe`'s `Store` opened #314's old slot-11 store, failed with `OperationalError: no such column: wake_state`, and left versions `[11, 12]` with no wake columns.

  The other three were already there: `release-217-v10`, `wake-caller-307-v11` (#307's slot 11) and `review-subject-old-v11` (#314's old layout).
- **Before:** with this file run against `48dab4abe`'s code (a scratch tree), 12 failed and 5 passed. Among the failures are the old-#314 layout and the broken store.
- **After:** all 27 cases pass at head.

### P2: Sol was never chosen (`subfleet/policy.py:90`)

- **Cause:** the build used the default reviewer only when the tier's other-family suffix was empty.
- **Change:** `routing_chain` now walks `[the family's default, then the chain's other-family models from the tier upward]`, deduplicated, with the `--allow-same-family` tail last. `astra` is reached only after `sol61`. Demand, pins, `why` and the recheck use the same chain.
- **Evidence:** `test_p2_sol_first_routing_matrix` gives this table (shipped policy, one lane per family up, fable reserve off). This test replaces the reviewer's `test_probe_routing_matrix`, which lives in the reviewer's workspace:

| Tier | Claude caller | Codex caller | Unknown caller |
| --- | --- | --- | --- |
| trivial | `sol61 -> astra` (chosen `sol61`) | `opus -> haiku -> sonnet` (`opus`) | `haiku -> sonnet -> opus -> astra` (`haiku`) |
| easy | `sol61 -> astra` (`sol61`) | `opus -> sonnet` (`opus`) | `sonnet -> opus -> astra` (`sonnet`) |
| standard | `sol61 -> astra` (`sol61`) | `opus` (`opus`) | `opus -> astra` (`opus`) |
| hard | `sol61 -> astra` (`sol61`) | `opus` (`opus`) | `astra` (`astra`) |

`test_p2_astra_is_reached_only_when_sol_cannot_take_the_job` closes `sol61` on the Codex lane, and the walk becomes `sol61, astra`, choosing `astra`. I1's property (`test_i1_every_tier_family_and_chain_chooses_the_other_provider`) now expects the default first. All of these failed at the merge commit and pass at head. The I3 and I4 differentials against frozen A2 (null family, other tasks) are unchanged and pass.

### P2: the code defaults were never checked (`policy.py:77`, `:560`)

- **Cause:** only an explicit `review_family_defaults` entry was validated. Routing then resolved the unchecked code default and raised `pinned_model: unknown model 'sol61'`.
- **Change:**
  - `_usable_review_default` now runs the explicit key's checks (a known model, of the other family) on the code defaults too.
  - An explicit entry that fails still refuses the policy.
  - A code default that fails is dropped. The policy still loads, and `load_policy` stores and logs a warning (`_review_family_warnings`, written to `daemon.log`) naming the family and the tiers left with no reviewer.
  - `routing_chain` returns `[]` for such a family. `scheduler.prepare` keeps that chain empty instead of substituting the policy's first model, so the review waits with `waiting for other-family reviewer: policy names no codex reviewer (review_family_defaults or chains.review) from this tier upward`.
  - Submit accepts the job. It used to dereference a missing lane on an empty chain.
- **Evidence:**
  - In `test_review_family_fix_r1.py`: `test_p2_code_defaults_are_checked_at_load_and_a_policy_without_a_reviewer_loads`, `test_p2_a_review_with_no_other_family_reviewer_waits_naming_the_family` (four tiers) and `test_p2_a_policy_without_sol_but_with_astra_routes_claude_callers_to_astra`.
  - In `test_review_family_fix_r1_daemon.py`: `test_a_review_with_no_other_family_reviewer_is_accepted_and_waits`, which checks that `why` never says `unknown model`.
  - In `test_review_family_routing.py`: `test_i1_repurposed_default_name_never_selects_the_callers_provider` now expects the wait, not a `PolicyError`.

  All of these failed before the fix and pass after.

### P2: the Codex writer exemption trusted `caller_family` (`daemon.py:3989`)

- **Cause:** the exemption tested the self-declared family.
- **Change:**
  - The CLI sends `caller_session_kind`, the family whose harness variable supplied the session id: `CLAUDE_CODE_SESSION_ID` gives `claude`, `CODEX_THREAD_ID` and `CODEX_SESSION_ID` give `codex`, and `SUBFLEET_SESSION_ID` gives none. With both families' markers present, an id gets a kind only when the process ancestry names that same family. The kind never comes from `--caller-family` or `SUBFLEET_CALLER_FAMILY` (`cli.session_source` and `session_kind`, used by `run` and `resume`).
  - `daemon.codex_writer_exempt` skips the writer-instance comparison only for kind `codex` with a family that does not contradict it.
  - A contradiction keeps the check and is recorded on the job as `caller_family_disagreement` in `job.submitted`, which `show` returns.
  - A submission with no kind, as from an older CLI, keeps the check.
  - `caller_pid` now keeps `CLAUDE_PID` for a Claude session id whatever family is declared, so the check can still identify the instance.
- **Evidence:**
  - `test_claude_session_declaring_codex_keeps_the_writer_instance_check` (the reviewer's case: refused as `unidentified-writer`, with the disagreement recorded and shown).
  - `test_codex_family_without_a_codex_session_id_keeps_the_check`, `test_codex_thread_id_declaring_claude_is_a_recorded_disagreement_and_checked`, `test_a_codex_thread_id_keeps_its_exemption` and `test_an_invalid_session_kind_is_refused`.
  - The CLI cases `test_p2_session_kind_follows_the_marker_never_the_declared_family` (six environments) and `test_p2_mixed_markers_give_a_kind_only_when_ancestry_agrees`.

  The new cases failed at the merge commit and pass at head. `test_codex_writers.py` and `test_codex_writable_fanout.py` now carry the kind (the latter derives it from its synthetic `CODEX_THREAD_ID`), and both pass.

### P2: same-family consent was a flag anyone sets (`cli.py:3660`, `daemon.py:3195`)

- **Change:**
  - The daemon refuses `allow_same_family` on a submission that carries a parent job, other than a resume's source, with the new refusal `same-family-consent` (exit 7, before insertion).
  - The CLI refuses `run --allow-same-family` when `SUBFLEET_JOB`, `SUBFLEET_ATTEMPT` or `SUBFLEET_TURN_JOB` is set, or `--parent` is given. A lane job's `run` carries no parent unless it passes `--parent`, so the daemon check alone would catch little.
  - An accepted consent is recorded as `same_family_consent: {session, session_kind}` in `job.submitted`, and `show` and `runs show` return it.
  - C-6.23 now says what this does not stop: a desktop session can still consent, and so can a job that drops its markers and parent, or a client that writes its own frames.
- **Evidence:** `test_same_family_consent_from_inside_a_job_is_refused_before_insertion`, `test_same_family_consent_records_who_gave_it_and_show_says_so` and `test_p2_cli_refuses_same_family_consent_inside_a_job` (three markers). All failed before and pass after.

### P3: a same-family lane pin got the generic error (`scheduler.py:822`)

- **Change:** `prepare` raises `review-same-family: ...` for a review pinned to a lane of the caller's family without `-m`, and submit refuses it the same way (`_pin_family`). The `review-same-family` template now covers lanes.
- **Contract:** `--allow-same-family` does not lift a bare lane pin, because a pinned job walks only its chain's first model, the other family's reviewer. A same-family lane pin needs `-m` naming that family's model plus the flag.
- **Evidence:** `test_p3_a_same_family_lane_pin_is_a_review_same_family_refusal` (both families, with and without the flag) and `test_a_same_family_lane_pin_is_refused_at_submit` failed before and pass after. `test_p3_same_family_lane_pin_with_a_same_family_model_and_consent_runs` is the positive control and passes before and after.

### P3: no test of the family-naming reason on the reservation recheck (`route_check.py:312`)

`test_p3_reservation_recheck_keeps_the_family_naming_wait_reason` drives `route_check.still_stands` through `test_route_check.check`: a concurrent reservation fills the fleet, and the recheck's reason must name `missing codex capacity` and equal a fresh evaluation's.
- **Mutant:** deleting the `required_review_family=` argument at `route_check.py:312`, in the scratch tree, fails it (1 failed: the reason lacks the family clause).
- **Restore:** the file was restored byte-identical (SHA-256 prefix `3bec93c45651e452` before and after; `cmp` equal).

### Commands and results

Every pytest run used `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=<that>sf-rva3-fix1/uv-cache`, `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`, with a new `--basetemp` under scratch named `pytest-*`. Workers stayed at four or fewer: runs started together shared that budget.

| Command | Result |
| --- | --- |
| `date -u` | 04:10:11 UTC |
| `uv sync --group dev` | exit 0, 49.7 s |
| `git merge --no-ff --no-commit 2954abd77` | 9 conflicts, resolved as above; committed as `127576d13` |
| Merge check, `-n 4`: upgrade slots, family/subject/numbered/QoS migrations, prune, reserve plumbing, store ids (8 files) | 135 passed, 487 s |
| `make_fixture.py` for `2954abd77`, `48dab4abe` and `48dab4abe` over old #314 | 3 fixtures; the last reproduced P1 |
| Red, `127576d13` tree, `-n 2`: new unit + daemon files, routing | 14 failed, 23 passed, 1 collection error (the unit file imported a name that did not exist yet), 153.7 s |
| Red, `48dab4abe` tree: `test_store_upgrade_slots.py` | 12 failed, 5 passed, 27.2 s |
| Green 1: new unit + daemon files | 33 passed, 4 failed (the lane's own `SUBFLEET_JOB` leaked into the CLI cases; the fable reserve blocked the synthetic Claude lane), 54.8 s |
| Red 2, `127576d13` tree, `-n 2`: new unit file, now collectible | 22 failed, 3 passed, 59.8 s |
| Green 2, `-n 2`: 9 new and changed files | 119 passed, 2 failed (`test_review_family_cli.py`'s fake daemon lacked #314's capability), 248.1 s |
| Recheck mutant (scratch tree) | 1 failed, 62.6 s; restored byte-identical |
| `tests/fake/test_state_contract.py` | 37 passed, 208.6 s |
| `test_test_module_names.py`, `test_invariants_index.py`, `test_handler_refusals.py`, `test_protocol_registry.py` | 418 passed, 259.0 s |
| Subject/numbered/QoS migrations, prune, reserve plumbing, store ids, retention subject (7 files) | 124 passed, 316.0 s |
| Family admission, gate family, caller ancestry, subject CLI, policy support, unadmittable pin (6 files) | 118 passed, 96.1 s |
| `-n 4`: CLI, policy, route check, writable fan-out, subject validation/submission/resume (CLI and fake), gate admission, gate service (10 files) | 640 passed, 511.4 s |
| Final, `-n 4`: family CLI, CLI, both new files, caller ancestry, CLI batch (6 files) | 334 passed, 54.7 s |

After the fixes, the latest run of each of the 38 test files passed: no test fails at head. The runs largely followed the brief's order. The new and changed tests ran first; the four required files ran beside the store and migration files; the importing files and the final CLI run came after them.

### What did not run, and deviations

- **Importing files not run.** `git grep -l` with an import parse finds 354 test modules that import a changed module (`policy`, `scheduler`, `daemon`, `cli`, `protocol`, `refusals`, `store`). This round ran 38 of them, so 316 did not run here; CI runs the whole suite.
- **`tests/unit/test_host_push.py` was not run.** Its fixtures push to local bare repositories, a write-shaped command the brief forbids even behind a stub.
- **Red evidence ran in scratch trees, not this checkout.** The trees were `git archive 127576d13` and `git archive 48dab4abe` (`subfleet`, `tests`, `pyproject.toml`) with the new test files copied in. This shows each test failing on the old code without reverting the checkout.
  - The first red run of the new unit file was a collection error. After a one-line import change it ran per test.
  - Three new unit cases pass on the merge code by design: the recheck test, whose red is the mutant; the lane-pin positive control; and `test_p2_explicit_same_family_or_unknown_default_still_fails_load`, which the build already satisfied.
- **The reviewer's probe file (`test_zz_rv_a3_r1_probe.py`) is in the reviewer's workspace.** I wrote `test_p2_sol_first_routing_matrix` instead of rerunning the reviewer's `test_probe_routing_matrix`.
- **`tests/unit/conftest.py` now also drops `SUBFLEET_JOB`, `SUBFLEET_ATTEMPT` and `SUBFLEET_TURN_JOB` from the "outside any session" environment.** These tests run inside a Subfleet job, whose markers would otherwise trip the new consent refusal.
- **The stale-notice backfill is gone** (see the merge above). That follows D-SCHEMA-SLOTS1 and changes what a direct 10 → head upgrade leaves in `notices`. The link-7 merge brought the backfill back (see [Link-7 merge and the migration test](#link-7-merge-and-the-migration-test-670653bf9-e0bb74d2f-4d52eca3e)).
- **Offline `runs show` (daemon down) shows no caller record.** `same_family_consent` and `caller_family_disagreement` come from the daemon's `show`.
- **Probe commands created cache folders in the checkout.** Early probes ran without `PYTHONDONTWRITEBYTECODE` and created `subfleet/__pycache__`, `subfleet/sessions/__pycache__` and `tests/__pycache__`; the test runs created `.hypothesis` and `uv sync` created `.venv`. All five are listed under cleanup below.
- **Parallel runs.** Several pytest runs ran at the same time, each in the foreground, with at most four workers in total.
- **Read other sessions' processes.** My first `git status` stalled for about two minutes. To diagnose it, I read `pgrep`/`ps` output for other sessions' `git status` processes. That was read-only and signalled nothing, but the brief says never to read a live process.
- **The CLI's consent refusal carries no reason code.** C-6.5 names `same-family-consent` for both refusals, but only the daemon's carries that reason; the CLI's returns exit 7 with its message.

### Cleanup

I made these folders outside scratch:
- In the checkout: `.venv` (from `uv sync`), `.hypothesis` (from the test runs), and `subfleet/__pycache__`, `subfleet/sessions/__pycache__` and `tests/__pycache__` (from the probe commands).
- The `/tmp/sf-*` roots, which the unit conftest's `root` fixture makes and removes itself.

The scratch root, `$(getconf DARWIN_USER_TEMP_DIR)sf-rva3-fix1`, held `arch/`, `red/`, `uv-cache`, every `pytest-*` basetemp, `scripts/make_fixture.py` and `route_check.pristine.py`. I moved the five checkout folders into `sf-rva3-fix1/checkout-cleanup/`, then deleted the scratch root with a `shutil.rmtree` that asserts `sf-rva3-fix1` is in the path.

The deletion succeeded: all five checkout folders were moved, and the scratch root no longer exists. `ls -a` of the checkout shows only tracked entries (including the tracked `.coverage`), and no `__pycache__` folder remains. No process of mine is still running. I finished at 2026-11-17 06:42:47 UTC, within the three-hour limit.

## Fix round 2

Started 2026-11-26 at 11:26:52 UTC. The assigned checkout was clean and detached at `2cc36dc5e3ae`. Git could not create `index.lock` for the assigned worktree. The fixes and report are in the worktree for the hub to collect.

| Finding | Cause and change | Red at `2cc36dc5e` and green with the fix |
| --- | --- | --- |
| P1.1 Empty chain with a lane pin | The scheduler substituted the pinned provider's first model when the policy supplied no independent reviewer. An empty independent chain now waits with the missing family in its reason, including with a stored bare lane pin. A new same-family bare pin still gets its admission refusal. | `test_bare_other_family_lane_pin_preserves_empty_review_chain` saw Astra in the baseline `why JOB` chain. It now sees an empty chain and the Codex policy reason. Eight scheduler cases cover stored bare pins across both lane families and all four tiers. |
| P1.2 Matching replay | The model, consent and bare-lane rules ran before the request-id lookup. Submit now resolves matching replays and digest conflicts before those checks. The digest retains the consent bit and excludes caller identity. | `test_review_request_replay_uses_the_accepted_job` covers all three checks. The same requests return the original id with `created=false`, changed digests still conflict, and new ids are refused. |
| P1.3 Codex handoff | Both handoff commands sent the session id without its kind. They now send both together through `Sessions.submit`. Revive uses the revived session's id and still enforces distinct-instance exclusion. | `test_native_codex_handoffs_keep_writer_fanout` ran the direct handoff and cold-continue commands. The baseline created the first job and refused the second as an unidentified writer. Both targets are admitted with kind `codex` on the fixed code. |
| P1.4 Mixed-version CLI | `review.subject.v1` predated the family rules. `capabilities` now advertises `review.family.v1`. An affected run, batch, resume, handoff, review shape explanation or new gate start checks for it before sending the affected operation. Resume and handoff preflight before their source reads. | `test_review_family_boundary.py` and `test_handoff_requires_family_support_before_session_reads` exercise subject-only stand-ins. A missing capability yields exit 69 and sends only `capabilities`. The unattributed review control works on the older daemon. |
| P1.5 Shipped models | The shipped defaults selected Sol 6.1 followed by Astra. Code and shipped policy now use `sol62` (`gpt-6.2-sol`, ultra) for Claude callers and Opus for Codex callers. Unattributed hard reviews try Sol 6.2 then Opus. Explicit pins and stored jobs can still resolve the `sol61` and `astra` model entries. | `test_probe_routing_matrix` and the four cases of `test_live_policy_variants_as_implemented` fail at baseline and pass with the fix. Results follow below. |
| P1.6 E2E fixtures | The shared E2E CLI environment represents a Claude caller. The Claude identity and Haiku publish cases now declare a Codex caller, and the cross-provider capacity-steer case clears caller attribution. | The in-process replays `test_e2e_identity_route_names_rejected_claude_lane`, `test_e2e_publish_can_submit_haiku_review` and `test_e2e_generic_review_steers_between_providers` fail under the original fixture context. With the updated contexts, `why` names and skips the mismatched Claude lane, the Haiku review submits, and the synthetic Astra capacity refusal sends the next attempt to Opus. |
| P2.1 Ambiguous `why` | Shape `why` resolved an ambiguous mixed caller to null. It now shares `run`'s family helper and refuses unresolved review callers with the `--caller-family` remedy. | `test_why_and_run_refuse_ambiguous_review_family` changes from exit 0 for `why` to exit 7 for both commands. |
| P2.2 Steered chain | An empty result after steering used the missing-policy reason. The decision now names the steered models and their steer expiry. The next-check clock still uses that expiry. | `test_fully_steered_review_explains_what_happened` fails on the policy diagnosis at baseline and passes with the steer reason and clock. |
| P2.3 Offline attribution | Offline `runs show` recovered the consent bit from the job row and skipped the related event keys. Both show paths now return the session kind, consent and disagreement from `job.submitted`. | `test_offline_show_keeps_the_online_review_attribution` fails on the absent fields at baseline and passes with matching metadata. |
| P2.4 Resume remedy | Resume reused the model-pin advice from `run`. It now advises resuming from an other-family session or submitting a fresh review with consent. | `test_resume_same_family_refusal_suggests_a_fresh_review_or_other_session` fails on the old flags and passes with the new advice and exit 7. |
| P3.1 Refusal names | The socket and client filtered away both reason names. They now preserve `review-same-family` and `same-family-consent`. Local consent refusal uses the same wording and names the reason. C-6.5 records the resume-source exception. | Both cases of `test_review_refusal_reason_crosses_the_socket` and `test_cli_consent_refusal_names_the_same_reason` fail at baseline and pass with the fix. |
| P3.2 Load warning | The README implied that every unusable code default produced a warning. It now describes the tier whose chain has no other-family reviewer after the default is dropped. | `test_readme_limits_warning_to_empty_other_family_tier` detects the old wording. The legacy custom-policy control still finds no warning when a tier has an alternative. |

### Shipped routing matrix

Each cell lists the first viable model for a Claude caller, a Codex caller, and an unattributed review, in that order. The probe also checks the full chain at every tier.

| Tier | Both families open | Claude unavailable | Codex unavailable | Only Sol 6.2 unavailable |
| --- | --- | --- | --- | --- |
| trivial | `sol62` / `opus` / `haiku` | `sol62` / wait / `sol62` | wait / `opus` / `haiku` | wait / `opus` / `haiku` |
| easy | `sol62` / `opus` / `sonnet` | `sol62` / wait / `sol62` | wait / `opus` / `sonnet` | wait / `opus` / `sonnet` |
| standard | `sol62` / `opus` / `opus` | `sol62` / wait / `sol62` | wait / `opus` / `opus` | wait / `opus` / `opus` |
| hard | `sol62` / `opus` / `sol62` | `sol62` / wait / `sol62` | wait / `opus` / `opus` | wait / `opus` / `opus` |

### Four policy variants

The replay uses the reviewer's variant setup at every tier for a Claude caller. With Sol 6.2 closed the review waits for Codex capacity in all four variants.

| Variant | Baseline first choice → fallback from the review | Fixed first choice → fallback |
| --- | --- | --- |
| a. Shipped with the family-default key removed | `sol61` → `astra` | `sol62` → wait |
| b. Add `sol62`, keep the chain and omit the key | `sol61` → `astra` | `sol62` → wait |
| c. As b, set the hard review slot to `sol62` | `sol61` → `sol62` | `sol62` → wait |
| d. Shipped with its explicit family-default key | `sol61` → `astra` | `sol62` → wait |

### PR body for the hub

- The configured other-family default goes first at every tier, followed by eligible other-family models from that tier upward. The defaults are Sol 6.2 for Claude callers and Opus for Codex callers. The shipped unattributed hard route has an Opus fallback. See `subfleet/policy.py:74`, `subfleet/policy.py:136` and `subfleet/default_policy.json:26–30`, `subfleet/default_policy.json:63–65`, `subfleet/default_policy.json:115`.
- Writer exemption follows a native Codex kind attached to the caller's session id, provided the declared family agrees or is absent. The daemon still enforces target conflicts, revive exclusion and configured caps. Both handoff commands carry the session kind. See `subfleet/daemon.py:757`, `subfleet/daemon.py:4205`, `subfleet/daemon.py:4217`, `subfleet/sessions/cli.py:460` and `subfleet/sessions/cli.py:683`.
- Slot 13 adds the `allow_same_family` boolean after #314's slots 11 and 12. The column is presence-checked on writable open, including for a store already recorded at 13. See `subfleet/store.py:48`, `subfleet/store.py:155`, `subfleet/store.py:164–165` and `subfleet/store.py:512`, `subfleet/store.py:524`.

### Test commands and results

I used `uv sync --group dev` for the missing development environment. Python was 3.15.2. The baseline probes ran from a `git archive` of `2cc36dc5e` with the probe files copied in. The routing tests reconstruct the review's matrix and four variants from the recorded setup. The E2E premise probes use the original caller context in the archive and the updated context in the worktree, with synthetic lanes and a synthetic capacity refusal.

Each run used `uv run --project WORKTREE --no-sync --with 'pytest-xdist>=3.6' python -m pytest -p no:cacheprovider --basetemp "$scratch/pytest-$run" -n WORKERS -q --tb=short -r f` and the selectors below. Workers were four, except two for `red-corrected` and one for the frontend batch. `green` and the runs from `red-corrected` through `contract` used `-o tmp_path_retention_policy=failed`. The runs from `red-corrected` onward loaded a scratch-only plugin to keep failed harness roots and their pruning inside the job's scratch directory.

| Run | Pytest selectors |
| --- | --- |
| `red` | `tests/unit/{test_review_family_fix_r1,test_review_family_routing,test_review_family_boundary}.py` and `tests/fake/{test_review_family_fix_r1_daemon,test_review_family_fix_r2_daemon}.py` |
| `red-extra` | `tests/fake/test_rva3_fixture_replays.py`, `tests/unit/test_rva3_readme.py`, `tests/fake/test_tier_hold_by_demand.py::test_c6_9_demand_models_follow_the_chain_evaluate_walks`, `tests/fake/test_review_family_admission.py::test_i2_admission_waits_without_other_family_and_override_permits_same_family`, `tests/unit/test_gate_review_family.py` |
| `red-fixtures-pins` | `tests/fake/test_rva3_fixture_replays.py tests/unit/test_review_family_fix_r1.py -k 'test_e2e or (waits_naming_the_family and (same-family-lane or other-family-lane))'` |
| `red-handoff` | `tests/fake/test_review_family_fix_r2_daemon.py::test_handoff_requires_family_support_before_session_reads` |
| `green` | The five `red` files, the fixture and README probes, `tests/fake/{test_tier_hold_by_demand,test_review_family_admission,test_admission_held_waiters,test_admission_properties,test_gate_end_to_end}.py`, and `tests/unit/{test_gate_review_family,test_pr124_fixes,test_caller_family_ancestry,test_cli,test_review_family_cli,test_review_subject_cli}.py` |
| `red-corrected` | `tests/fake/test_review_family_fix_r1_daemon.py::test_bare_other_family_lane_pin_preserves_empty_review_chain`, `tests/fake/test_review_family_fix_r2_daemon.py::test_native_codex_handoffs_keep_writer_fanout`, `tests/unit/test_cli.py::test_wait_backs_off_when_the_long_poll_returns_at_once`, `'tests/unit/test_cli.py::test_ping_open_pipe_is_bounded[]'` |
| `green-corrected` | The `red-corrected` selectors and `tests/fake/test_gate_end_to_end.py` |
| `imports-*` | `-m 'not real_lsof and not real_cwd' @"$scratch/$run.args"`. Each args file contained its sorted batch of direct importers from `git grep -l` and a Python import parse, with the exclusions below. This covered 423 files in the importer batches. Store and contract importers were deferred to their named batches. |
| `store` | `tests/unit/test_store_*.py` |
| `contract` | `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py` |
| `followup-red` | `tests/unit/test_policy_ramp.py::test_shipped_policy_preserves_raw_hash_and_disabled_ramp_defaults`, `'tests/unit/test_provider_active_cap.py::test_missing_cap_preserves_bytes_and_hash[None]'`, and the `test_before_its_start_json_the_guardian_starts_nothing_that_carries_the_attempts_markers` and `test_a_relay_that_starts_a_process_before_start_json_fails_the_premise` nodes in `tests/unit/test_quar_start1_census.py` |
| `followup-green` | The two policy hash nodes from `followup-red`, `tests/unit/test_scheduler.py::test_lane_pin_provider_absent_from_valid_policy_is_key_named_invalid_input`, `tests/unit/test_weekly_rank_vs_base.py`, `tests/fake/test_canonical_identity_fix5.py::test_resolver_propagates_nonmissing_errors`, and the two routing probes in `tests/unit/test_review_family_fix_r1.py` |

The importer batches ran all eight frontend files in one run, four unit batches of 87, 87, 87 and 16 files, then four fake batches of 37, 37, 37 and 27 files. The four required contract files passed all 453 cases, and the store and migration batch passed all 346 cases.

The first head run exposed harness issues in the new probes involving the empty tuple returned by in-process `why`, conversation state in the handoff stand-in, and capability discovery in the gate stand-in. The corrected probes isolated the expected baseline failures and passed with the fixes. Both CLI timing cases from the first head run passed in `green-corrected`, and the long-poll case also failed on the baseline under load. The three guardian cases in `test_quar_start1_census.py` failed on both trees because the sandbox could not obtain the macOS boot identity. The two canonical-identity teardown errors came from pytest's scratch retention cleanup calling a patched `lstat`. Both passed with normal retention in `followup-green`.

The code paths outside the original PR diff are needed by the fixes. The client allowlist carries the refusal names through decoding and lost-answer handling. The sessions helper and client carry handoff kind and discovery through the normal call path. Offline show reads the submission event. Outside the original test diff, capability stand-ins advertise the new capability, historical routing fixtures keep their original overlap or scalar-tier premise, the stateful oracle handles a tier with alternatives, and policy hash fixtures use the shipped bytes. The late fixture updates were checked in the importer runs or `followup-green`. Fixtures that preserve an existing premise are positive controls. The contract has one change-log entry for the round.

| Run | UTC start–end | `df -H /` free / capacity | `uptime` load at start → end | Passed / failed / skipped / deselected reported | Basetemp |
| --- | --- | --- | --- | --- | --- |
| `red` | 11:38:50–11:42:22 (late end sample) | 77G / 16% | 66.45, 76.96, 74.82 → 60.92, 68.18, 71.20 | 57 / 33 / 0 / 0 | 12M |
| `red-extra` | 11:42:26–11:44:44 | 77G / 16% | 60.92, 68.18, 71.20 → 79.85, 72.88, 72.57 | 17 / 8 / 0 / 0 (3 errors) | 3.8M |
| `red-fixtures-pins` | 11:45:41–11:47:23 | 77G / 16% | 39.81, 62.79, 68.83 → 20.87, 48.81, 62.63 | 0 / 11 / 0 / 0 | 1.7M |
| `red-handoff` | 11:47:43–11:48:49 | 77G / 16% | 21.21, 47.06, 61.68 → 27.57, 43.78, 59.35 | 0 / 4 / 0 / 0 | 0B |
| `green` | 11:50:54–11:58:45 | 77G / 16% | 56.87, 48.14, 58.81 → 34.06, 43.89, 53.96 | 536 / 10 / 1 / 0 (1 xfailed) | 38M |
| `red-corrected` | 12:02:27–12:04:16 | 79G / 15% | 63.72, 46.06, 51.75 → 81.39, 57.37, 55.50 | 1 / 4 / 0 / 0 | 1.7M |
| `green-corrected` | 12:05:55–12:07:23 | 75G / 16% | 105.16, 70.62, 60.76 → 41.98, 60.30, 57.90 | 15 / 0 / 1 / 0 | 0B |
| `imports-frontend-01` | 12:08:22–12:11:00 | 73G / 16% | 41.95, 56.72, 56.71 → 28.86, 47.15, 52.92 | 129 / 0 / 0 / 0 | 15M |
| `imports-unit-01` | 12:11:00–12:18:43 | 72G / 17% | 28.86, 47.15, 52.92 → 25.43, 31.88, 42.33 | 4604 / 0 / 10 / 0 (8 xfailed) | 59M |
| `imports-unit-02` | 12:18:52–12:22:23 | 70G / 17% | 25.56, 31.79, 42.24 → 20.87, 27.02, 37.97 | 3407 / 1 / 9 / 0 (60 xfailed) | 15M |
| `imports-unit-03` | 12:22:25–12:33:24 | 71G / 17% | 20.87, 27.02, 37.97 → 19.16, 20.52, 27.72 | 3976 / 5 / 4 / 0 (16 xfailed) | 52M |
| `imports-unit-04` | 12:33:25–12:35:19 | 71G / 17% | 19.16, 20.52, 27.72 → 9.38, 16.72, 25.32 | 491 / 6 / 0 / 0 | 0B |
| `imports-fake-01` | 12:35:19–12:37:17 | 71G / 17% | 9.38, 16.72, 25.32 → 8.39, 14.44, 23.37 | 757 / 0 / 9 / 0 (2 errors, 10 xfailed) | 323M |
| `imports-fake-02` | 12:37:20–12:38:07 | 71G / 17% | 8.04, 14.27, 23.25 → 8.37, 13.50, 22.50 | 721 / 0 / 7 / 0 | 69M |
| `imports-fake-03` | 12:38:08–12:39:21 | 71G / 17% | 8.37, 13.50, 22.50 → 8.30, 12.43, 21.34 | 624 / 0 / 8 / 0 | 104K |
| `imports-fake-04` | 12:39:21–12:40:07 | 71G / 17% | 8.30, 12.43, 21.34 → 7.48, 11.63, 20.58 | 597 / 0 / 1 / 0 | 71M |
| `store` | 12:40:07–12:40:40 | 71G / 17% | 7.48, 11.63, 20.58 → 5.66, 10.73, 19.90 | 346 / 0 / 0 / 0 | 0B |
| `contract` | 12:40:40–12:41:01 | 71G / 17% | 5.66, 10.73, 19.90 → 4.61, 10.16, 19.48 | 453 / 0 / 0 / 0 | 0B |
| `followup-red` | 12:41:20–12:41:22 | 71G / 17% | 3.81, 9.62, 19.07 → 3.81, 9.62, 19.07 | 0 / 5 / 0 / 0 | 8.0K |
| `followup-green` | 12:41:22–12:41:29 | 71G / 17% | 3.81, 9.62, 19.07 → 3.83, 9.43, 18.89 | 39 / 0 / 0 / 0 | 28K |

All `df -H /` samples reported 3996G total and 14G used. Counts are as reported by pytest. Under xdist, worker-side deselections may be absent from the summary. The first red run finished pytest but hit zsh's read-only `status` variable before recording end uptime. Its end sample and basetemp size were recorded during recovery. The three setup errors in `red-extra` came from a missing fixture import in the scratch replay and were superseded by the three behavioral failures in `red-fixtures-pins`.

### Scope and deviations

- All review findings assigned to this round have a passing probe. The three guardian boot-identity cases remain blocked in this sandbox and failed with the same error on the starting revision.
- E2E and process files and the whole-suite run were left to CI. The E2E premises ran in process.
- The importer run excluded real process-listing or cwd-inspection markers, four `test_gate_service.py` cases that invoke merge, the GraphQL transport case `test_batched_gh_query_reads_all_targets_once_and_does_not_treat_partial_checks_as_finished`, and the two keychain launch cases `test_probes_detect_a_launch_that_is_not_refused` and `test_python_child_of_the_session_is_guarded_from_import`.
- The importer run omitted `tests/unit/test_host_push_{admission,build,mutations,restart,storage_recovery,validated_top}.py`. It selected command-as-data or state-only nodes in the other host-push importer files, omitting the local-push setup, fake transport timeout push, and direct push cases as applicable.
- The initial archive included the full docs directory and briefly exceeded the 200 MB scratch target. I removed that directory from the archive and restored only the acceptance contract. The first fake importer batch also exceeded that target while the stateful tests retained temporary state. Its basetemp measured 323M and was deleted after the run. The unit 03 basetemp contained read-only fixture directories. I restored owner permissions within that basetemp and retried the guarded deletion.
- The sandbox denied creating scratch at the Darwin temp root. I used the job-provided temp directory and its job-local uv cache. The initial pytest runs used the repository's default failed-harness pruning hook. The subsequent runs redirected it to scratch. No pruning outside scratch was reported.

### Cleanup

The job temp directory was `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261126-122333-rva3-fix-r2-a1-eg508r4o`. Its `sf-rva3-fix2` scratch directory held the baseline archive, temporary probes, test logs, and each `pytest-<run>` basetemp listed above. The harnesses removed their short-lived `/tmp/sf-*` state directories.

The following remaining directories are relative to that job temp directory. I recorded them before moving them under `sf-rva3-fix2` for guarded removal.

- `pytest-of-maxghenis/`
- `sf-64b4175f-753o5x0y/`
- `sf-64b4175f-h0gw6kmb/`
- `sf-64b4175f-rwa4wmi0/`
- `sf-f3bffcea-3e75a71g/`
- `sf-f3bffcea-4tf7mk6r/`
- `sf-f3bffcea-avtkigu8/`
- `sf-f3bffcea-hbdq__kt/`
- `sf-f3bffcea-la77zir7/`
- `sf-f3bffcea-qzul4t9a/`
- `sf-f3bffcea-x8v3u59k/`
- `sf-test-homes-1_d2hwsw/`
- `sf-test-homes-38z2ivvs/`
- `sf-test-homes-kktzfnrl/`
- `sf-test-homes-lncri6n2/`
- `sf-test-homes-n6f0c1ec/`
- `sf-test-homes-nkvmebxh/`
- `sf-test-homes-rynvdcyx/`
- `sf-test-homes-v72dfjly/`
- `sf-test-homes-vxezre98/`
- `uv-cache/`

Generated checkout directories moved into scratch for the same cleanup were `.venv/`, `build/`, `subfleet/__pycache__/`, `subfleet/adapters/__pycache__/`, `subfleet/conversations/__pycache__/`, `subfleet/gate/__pycache__/`, `subfleet/sessions/__pycache__/`.

Guarded removal succeeded. Finished 2026-11-26 at 12:45:08 UTC, within the job budget. The assigned checkout's Git head is still `2cc36dc5e3ae1f0454cbcb8d6038f6a356c8852e`.

no commit. The sandbox denied Git's `index.lock`.

## Link-7 merge and the migration test (670653bf9, e0bb74d2f, 4d52eca3e)

`670653bf9` took #314's link-7 head `8af8616a1`, which contains release/217 at `a4d21501e` and the November 26 chain of #313, #315, #321, #307, #329 and #316. #307 supplied the caller-wake implementation and its migration backfill.

The hub resolved eight conflicted files by hand. `git show --stat` and `git show --remerge-diff 670653bf9` record them.

- `subfleet/daemon.py`
- `subfleet/store.py`
- `tests/unit/test_policy_ramp.py`
- `tests/unit/test_provider_active_cap.py`
- `tests/unit/test_prune_217.py`
- `tests/unit/test_store_migration_2.py`
- `tests/unit/test_store_upgrade_slots.py`
- `docs/acceptance-contract.md`

The backfill runs in whichever step adds `notices.wake_state` (C-15.9). A direct upgrade from schema 10 marks the notices pending or offered at that step `stale` with reason `before-migration`. When the column is already present, no notices receive new marks. See `subfleet/store.py:536–542`.

`e0bb74d2f` corrected the report's behavior section to cite the review-subject clause as C-3.9. #315 owns C-3.8. `4d52eca3e` changed `tests/unit/test_store_review_family_migration.py::test_i6_schema_10_and_actual_pr307_11_reach_identical_schema_and_keep_existing_rows` to expect the stale marks and to compare the final notices across the direct and #307 upgrade paths. The previous assertion expected an empty wake state after direct upgrade.

The store matrix in section 3 of `review-a3-r3.md` covered 19 starting layouts. Every layout reached schema 13, preserved seeded rows and made no changes on a second open. Stale marks appeared on the notices that were pending or offered when the column was added. The review supplied this matrix.

## Fix round 3

Started at 05:46:27 UTC on 2026-11-28 on `4d52eca3e4b16069d825fe0af75c5ea801c8e33e`. The tracked check was clean and the untracked check listed no files.

### P2 · Gate peer resolution

The CLI omitted `sol62`, and both daemon start and preview hard-coded the old `sol` spelling to `astra`. Preview also accepted arbitrary wire peers. Starts and previews now share a peer resolver that calls `resolve_model` with `key="peer"` and `note=False`. It accepts the resolved names `fable`, `sol62` and `astra`. Invalid peers return exit 2 with the supported choices and the policy's reason when the model is unknown. Resolution runs before gate state, jobs or events are written.

The CLI accepts `--peer sol62`. For local previews and the pre-transport `sol` note, it reads the state-root policy, falling back to the shipped file when the state-root file is absent. The shipped `retired.sol` now maps to `sol62`, so policy-based handoff resolution follows it. Both policy hash tests pin the new raw SHA-256, `299189fa51b5dfc4bfbf2f1e435cc642bb55671db51ca1df0e1ffee9c748d042`. C-6.24, its change log and the gate examples describe the new peer. C-11.1 and the other clauses needed no edits.

The functional rerun on the base, `gate-policy-red-isolated`, had 19 failures and five passing controls. `gate-policy-green` passed all 24. Wire cases override the parsed peer in the payload to reach the daemon on the base.

| Node ID, with shared module prefix where shown | Base result | Patched result |
| --- | --- | --- |
| `tests/unit/test_gate_review_family.py::test_claude_gate_submits_sol62[explicit]` | Exit 2 from the daemon's previous allowlist | Records `sol62`, requests `gpt-6.2-sol`, and submits a `sol62` pin with `caller_family="claude"` |
| Same module, `test_claude_gate_submits_sol62[shipped-alias,live-alias]` | Both store `astra` | Both store and pin `sol62` with the same round model and caller family |
| Same module, `test_daemon_preview_resolves_retired_sol[shipped,live]` | Both previews report `astra` | Both report `sol62` without writing gate state, events or jobs |
| Same module, `test_daemon_preview_refuses_unsupported_peer[missing-peer,sol61]` | Both wire requests return 0 and echo the peer | Both return 2 and name the gate choices, with no writes |
| Same module, `test_gate_refuses_missing_sol62_policy_model` | Exit 2 and no writes because the old allowlist rejects every `sol62` | Exit 2 and no writes with the policy reason `peer: unknown model 'sol62'` |
| Same module, `test_recorded_caller_family_refuses_same_family_peer[claude-fable]` | Passes | Passes. The message names different model families, with no job or event |
| Same module, `test_recorded_gate_family_is_retained_on_peer_job[claude-astra]` | Passes | Passes. Explicit Astra remains accepted for Claude |
| `tests/fake/test_gate_end_to_end.py::test_sol62_gate_round_attests_the_requested_model` | Exit 2 from the daemon's previous allowlist | The parser accepts `sol62` and the round's requested model matches the admitted attempt's `gpt-6.2-sol` through attestation. The next round uses the recorded peer and completes |
| Same fake module, `test_cli_preview_prints_one_policy_alias_note[shipped,live]` | Reports `astra` and prints the Astra note | Reports `sol62` and prints one note naming it |
| Same fake module, `test_cli_start_prints_one_policy_alias_note[shipped,live]` | Prints the Astra note before transport | Prints one `sol62` note before transport and submits the `sol62` pin. The usual peer-job progress line follows |
| Same fake module, `test_cli_preview_prints_one_policy_alias_note[astra-map]` and `test_cli_start_prints_one_policy_alias_note[astra-map]` | Pass | Pass. The synthetic Astra map still gets one note naming Astra |
| `tests/unit/test_scheduler.py::test_claude_review_pin_routes_to_sol62` | Passes | Passes. With the shipped policy and one Codex lane, `evaluate` chooses `sol62` for the pin |
| `tests/unit/test_policy.py::test_retired_sol_alias_resolves_to_sol62_with_note` and `::test_retired_note_can_be_suppressed_for_repeat_evaluations`, `tests/unit/test_scheduler.py::test_retired_sol_alias_resolves_with_note`, `tests/unit/test_sessions_cli.py::test_the_retired_sol_target_still_parses` | Shipped alias gives `astra` | Shipped alias gives `sol62` |
| `tests/unit/test_policy_ramp.py::test_shipped_policy_preserves_raw_hash_and_disabled_ramp_defaults` and `tests/unit/test_provider_active_cap.py::test_missing_cap_preserves_bytes_and_hash[None]` | Old bytes fail new pins | Both match the new raw hash |

The attestation case fixes the fixture's admission decision to `sol62` so it checks the round record and the attempt model in the attestation path. The scheduler case above independently exercises routing. Two existing tests build synthetic policies without Sol, so their raw fixtures now remove the dependent alias before `load_policy` validates them. Those are `tests/unit/test_review_family_fix_r1.py::test_p2_code_defaults_are_checked_at_load_and_a_policy_without_a_reviewer_loads` and `tests/fake/test_review_family_fix_r1_daemon.py::test_bare_other_family_lane_pin_preserves_empty_review_chain`. Their routing assertions are unchanged.

### P2 · Admission comparison clock

`tests/fake/test_review_family_admission.py::test_i3_i4_durable_decision_rows_and_events_match_a2` already froze the daemon and store clocks. The admission passes still reached the live clock in `scheduler.waiting_metadata` when setting `next_check_at`. The test now patches `scheduler.datetime` with the same `FrozenDateTime`. It still compares the complete durable decision and event records.

This scratch pytest plugin delayed only the legacy daemon until about 50 ms past the next whole second. The base run failed all four `True` cases on `job.capacity_waiting.data_json.next_check_at`. `[True-build-claude]` and `[True-review-None]` differed by `05:52:05Z` versus `05:52:06Z`. `[True-build-None]` and `[True-build-codex]` differed by `05:52:06Z` versus `05:52:07Z`. The four `False` cases passed. All three patched probe runs passed all eight cases. The five stability runs passed 40 of 40 cases. Each stability run started above a one-minute load of 20. No other clock patch was needed.

### P3 · Link-7 migration history

The report at the base contained the fix-round-1 behavior and had no link-7 section. The link-7 section above records the merge, restored C-15.9 backfill, citation fix and updated migration test. Both historical bullets now point to it, and the round-2 PR-body notes use the checked citations below. The base evidence is the two unqualified historical statements, the missing section and the six stale citations. The updated report preserves both historical statements and adds the corrective links and citations. The reviewed 19-layout store matrix supplies the detailed migration evidence. The focused migration suite also exercises the direct schema-10 path and the existing-column path, with results in the runs table.

### Forced-second-boundary probe

```python
import math
import time

import pytest

from subfleet.daemon import Daemon


@pytest.fixture(autouse=True)
def force_admission_across_second(monkeypatch):
    original_admit = Daemon._admit

    def admit(daemon, *args, **kwargs):
        if daemon.root.name == "legacy":
            now = time.time()
            time.sleep(math.floor(now) + 1.05 - now)
        return original_admit(daemon, *args, **kwargs)

    monkeypatch.setattr(Daemon, "_admit", admit)
```

### PR body for the hub

Suggested replacements for the live draft at `2cc36dc5e`, starting with the sentence about `sol61`.

| Live copy | Replacement and evidence |
| --- | --- |
| The `sol61` fallback and the preceding sentence on `routing_chain` | The code defaults are `sol62` for Claude callers and `opus` for Codex callers. An attributed review tries the other family's usable default first at every tier, then that family's models from the requested tier upward. A policy can override the defaults with `review_family_defaults`. If the key is absent, the code reads its defaults without rewriting the policy. Recorded same-family consent appends the eligible unfiltered chain. See `subfleet/policy.py:74`, `subfleet/policy.py:87–100`, `subfleet/policy.py:136–153` and `subfleet/default_policy.json:63–65`. |
| Opening claim that a review goes to the other provider family | An attributed review goes to the other provider family by default. Without same-family consent, the job waits and names that family when it has no capacity. Recorded consent permits a same-family review. See `subfleet/policy.py:136–153` and `docs/acceptance-contract.md:564`. |
| Existing same-family pin refusal | An attributed review with an explicit same-family model pin requires consent. A same-family lane pin also needs an explicit model from that family. See `subfleet/daemon.py:3433–3443` and `subfleet/scheduler.py:808–820`. |
| Gate caller-family sentence | A new gate requires `review.family.v1`. It uses the detected caller family as the main author's even without `--main-model`, and requires a peer of the other family. Choose `fable`, `sol62` or `astra` directly. The retired `sol` alias follows the policy's `retired` map at start and in dry runs. The shipped map points `sol` to `sol62`, so a recorded Claude caller can use GPT-6.2 Sol. See `subfleet/gate/cli.py:41`, `subfleet/gate/cli.py:92–101`, `subfleet/gate/cli.py:116`, `subfleet/gate/service.py:54`, `subfleet/gate/service.py:118`, `subfleet/gate/service.py:204`, `subfleet/gate/service.py:229–246` and `subfleet/default_policy.json:115–122`. |
| Codex writer exemption | A recorded native Codex session kind skips only the session writer-instance comparison when the declared caller family agrees or is absent. Target conflicts, revive exclusion and configured caps still apply. Both new and cold-continue handoffs carry the session kind. See `subfleet/daemon.py:757–763`, `subfleet/daemon.py:4197–4218`, `subfleet/daemon.py:4236–4239`, `subfleet/sessions/cli.py:460` and `subfleet/sessions/cli.py:683`. |
| Migration 12 and slot 11's claim about runtime behavior | Slot 11 installs #307's notice-wake schema. When the upgrade adds `notices.wake_state`, it marks pending and offered notices stale. Slot 12 adds #314's review-subject fields. Slot 13 adds this PR's `allow_same_family` boolean. Writable opens reconcile all three by column presence. See `subfleet/store.py:46–48`, `subfleet/store.py:142–166`, `subfleet/store.py:524–542`, `subfleet/store_schema.sql:236–254` and `subfleet/daemon.py:6100`. |
| Clause numbers and single changelog entry | C-6.23 and C-6.24 state the review and gate rules. C-3.9 records the storage changes. C-6.5, C-6.21 and C-17.2 cover refusals, caller identity and CLI flags. The follow-up changes also cover replay order in C-6.2, public refusal reasons in C-16.1 and capability discovery in C-25.1. The changelog records the review-family work and its fix rounds. See `docs/acceptance-contract.md:497`, `docs/acceptance-contract.md:542`, `docs/acceptance-contract.md:546`, `docs/acceptance-contract.md:562`, `docs/acceptance-contract.md:564`, `docs/acceptance-contract.md:565`, `docs/acceptance-contract.md:684`, `docs/acceptance-contract.md:688`, `docs/acceptance-contract.md:700`, `docs/acceptance-contract.md:842` and `docs/acceptance-contract.md:9–11`. |

### Citation check

The round-2 PR copy cites the following lines. The audit used `git show HEAD:<path> | sed -n '<line>p'` for each line.

| Location | Evidence |
| --- | --- |
| `subfleet/policy.py:74` | code defaults, `REVIEW_FAMILY_DEFAULTS = {"claude": "sol62", "codex": "opus"}` |
| `subfleet/policy.py:136` | routing chain implementation, `def routing_chain(policy: Mapping[str, Any], task: str, tier_index: int = 0, *,` |
| `subfleet/default_policy.json:26` | review chain, `"review": [` |
| `subfleet/default_policy.json:30` | hard review fallback, `["sol62", "opus"]` |
| `subfleet/default_policy.json:63` | shipped family defaults, `"review_family_defaults": {` |
| `subfleet/default_policy.json:64` | Claude caller default, `"claude": "sol62",` |
| `subfleet/default_policy.json:65` | Codex caller default, `"codex": "opus"` |
| `subfleet/default_policy.json:115` | Sol 6.2 model entry, `"sol62": {` |
| `subfleet/daemon.py:757` | writer exemption definition, `def codex_writer_exempt(job: dict[str, Any]) -> bool:` |
| `subfleet/daemon.py:4205` | session writer loop, `for row in mine:` |
| `subfleet/daemon.py:4217` | exemption in the loop, `if codex_writer_exempt(job):` |
| `subfleet/sessions/cli.py:460` | cold-continue handoff session kind, `caller_session_kind=caller_session_kind,` |
| `subfleet/sessions/cli.py:683` | new handoff session kind, `caller_session_kind=caller_session_kind,` |
| `subfleet/store.py:48` | slot 13 constant, `REVIEW_FAMILY_MIGRATION = 13` |
| `subfleet/store.py:155` | slot 12 DDL, `REVIEW_SUBJECT_MIGRATION: (` |
| `subfleet/store.py:164` | slot 13 DDL, `REVIEW_FAMILY_MIGRATION: (` |
| `subfleet/store.py:165` | same-family column, `"ALTER TABLE jobs ADD COLUMN allow_same_family INTEGER NOT NULL DEFAULT 0 "` |
| `subfleet/store.py:512` | migration entry point, `def _migrate(self, version: int) -> None:` |
| `subfleet/store.py:524` | presence-aware slot loop, `for step in range(min(version + 1, NOTICE_WAKE_MIGRATION), SCHEMA_VERSION + 1):` |

### Test runs

Times are UTC. The uptime samples show the display time, user count and 1-, 5- and 15-minute loads. Every pre-run `df -k /System/Volumes/Data` sample reported `/dev/disk3s5`, 3,902,614,160 total blocks, 99% capacity and 17% iused. The table records used, available, iused and ifree for each sample. Counts are passed / failed / skipped / xfailed / deselected / errors.

| Run | Start → end UTC | Uptime at start → end | `df -k` used / available / iused / ifree | Counts | Basetemp KiB |
| --- | --- | --- | --- | --- | ---: |
| `gate-policy-red` | 05:51:00 → 05:51:34 | 0:51 1 user, load averages: 18.65 54.48 51.43 → 0:51 1 user, load averages: 29.90 53.27 51.11 | 3796000000 / 71200000 / 147964927 / 712738840 | 3 / 21 / 0 / 0 / 0 / 0 | 4264 |
| `boundary-red` | 05:51:55 → 05:52:06 | 0:51 1 user, load averages: 30.81 51.97 50.69 → 0:52 1 user, load averages: 30.16 51.13 50.41 | 3796000000 / 70800000 / 147960504 / 711353800 | 4 / 4 / 0 / 0 / 0 / 0 | 8824 |
| `gate-policy-red-serial` | 05:53:17 → 05:53:23 | 0:53 1 user, load averages: 25.69 45.79 48.47 → 0:53 1 user, load averages: 24.36 44.84 48.10 | 3796000000 / 72800000 / 147885536 / 728569760 | 3 / 21 / 0 / 0 / 0 / 0 | 4264 |
| `gate-policy-red-isolated` | 05:55:12 → 05:55:54 | 0:55 1 user, load averages: 24.34 38.29 45.12 → 0:55 1 user, load averages: 37.05 39.85 45.35 | 3796000000 / 72800000 / 147865224 / 729258520 | 5 / 19 / 0 / 0 / 0 / 0 | 4472 |
| `gate-policy-green` | 05:57:30 → 05:58:12 | 0:57 1 user, load averages: 26.08 35.39 43.05 → 0:58 1 user, load averages: 37.91 37.16 43.34 | 3796000000 / 72800000 / 147868956 / 729355840 | 24 / 0 / 0 / 0 / 0 / 0 | 4900 |
| `boundary-green-1` | 05:58:27 → 05:59:05 | 0:58 1 user, load averages: 38.97 37.46 43.33 → 0:59 1 user, load averages: 39.48 37.77 43.18 | 3796000000 / 72800000 / 147868324 / 728513920 | 8 / 0 / 0 / 0 / 0 / 0 | 9188 |
| `boundary-green-2` | 05:59:06 → 05:59:28 | 0:59 1 user, load averages: 39.48 37.77 43.18 → 0:59 1 user, load averages: 35.77 37.03 42.78 | 3796000000 / 72800000 / 147863080 / 729666240 | 8 / 0 / 0 / 0 / 0 / 0 | 9200 |
| `boundary-green-3` | 05:59:29 → 05:59:57 | 0:59 1 user, load averages: 34.91 36.83 42.68 → 0:59 1 user, load averages: 35.20 36.73 42.47 | 3796000000 / 72800000 / 147866330 / 729014840 | 8 / 0 / 0 / 0 / 0 / 0 | 9200 |
| `clock-stability-1` | 05:59:58 → 06:00:19 | 0:59 1 user, load averages: 35.20 36.73 42.47 → 1:00 1 user, load averages: 34.19 36.39 42.19 | 3796000000 / 72400000 / 147880446 / 725984000 | 8 / 0 / 0 / 0 / 0 / 0 | 8812 |
| `clock-stability-2` | 06:00:21 → 06:00:57 | 1:00 1 user, load averages: 34.19 36.39 42.19 → 1:00 1 user, load averages: 36.11 36.54 42.00 | 3796000000 / 72400000 / 147899105 / 724798720 | 8 / 0 / 0 / 0 / 0 / 0 | 9200 |
| `clock-stability-3` | 06:00:59 → 06:01:36 | 1:00 1 user, load averages: 37.70 36.87 42.08 → 1:01 1 user, load averages: 38.00 37.01 41.92 | 3796000000 / 72400000 / 147903915 / 724322880 | 8 / 0 / 0 / 0 / 0 / 0 | 9200 |
| `clock-stability-4` | 06:01:37 → 06:02:04 | 1:01 1 user, load averages: 38.00 37.01 41.92 → 1:02 1 user, load averages: 34.34 36.28 41.49 | 3796000000 / 72000000 / 147904644 / 722927520 | 8 / 0 / 0 / 0 / 0 / 0 | 8868 |
| `clock-stability-5` | 06:02:05 → 06:02:31 | 1:02 1 user, load averages: 34.34 36.28 41.49 → 1:02 1 user, load averages: 31.69 35.55 41.08 | 3796000000 / 72000000 / 147908871 / 721623320 | 8 / 0 / 0 / 0 / 0 / 0 | 9188 |
| `review-family-admission` | 06:02:31 → 06:03:01 | 1:02 1 user, load averages: 31.69 35.55 41.08 → 1:03 1 user, load averages: 31.38 35.20 40.77 | 3796000000 / 72000000 / 147912590 / 721722200 | 12 / 0 / 0 / 0 / 0 / 0 | 11024 |
| `affected-fake` | 06:03:24 → 06:05:19 | 1:03 1 user, load averages: 33.18 35.35 40.66 → 1:05 1 user, load averages: 30.68 33.43 39.21 | 3796000000 / 71600000 / 147915176 / 718610920 | 237 / 5 / 3 / 0 / 0 / 0 | 125324 |
| `affected-fake-serial` | 06:06:01 → 06:06:35 | 1:06 1 user, load averages: 38.59 35.06 39.54 → 1:06 1 user, load averages: 39.24 35.91 39.68 | 3796000000 / 71200000 / 147954277 / 715442040 | 5 / 0 / 0 / 0 / 0 / 0 | 2092 |
| `affected-unit-1` | 06:06:46 → 06:21:43 | 1:06 1 user, load averages: 35.45 35.21 39.39 → 1:21 1 user, load averages: 30.35 36.08 37.85 | 3796000000 / 71200000 / 147961456 / 714355520 | 3034 / 0 / 13 / 0 / 0 / 0 | 199120 |
| `affected-unit-2` | 06:23:21 → 06:45:16 | 1:23 1 user, load averages: 52.44 40.85 39.43 → 1:45 1 user, load averages: 32.09 30.04 32.12 | 3796000000 / 70800000 / 147948975 / 711078320 | 1834 / 0 / 0 / 0 / 0 / 0 | 208852 |
| `gate-service` | 06:46:03 → 06:46:34 | 1:46 1 user, load averages: 31.54 30.11 32.03 → 1:46 1 user, load averages: 34.20 30.99 32.27 | 3796000000 / 70800000 / 148002693 / 711763400 | 17 / 0 / 0 / 0 / 8 / 0 | 5324 |
| `contract-migration` | 06:46:36 → 06:47:44 | 1:46 1 user, load averages: 34.20 30.99 32.27 → 1:47 1 user, load averages: 22.34 27.70 30.91 | 3796000000 / 71200000 / 147934340 / 713954920 | 477 / 0 / 0 / 0 / 0 / 0 | 23868 |

The largest measured basetemp was 208,852 KiB (`affected-unit-2`). The largest end-of-run scratch sample was 218,272 KiB (`affected-unit-2`). The basetemps totaled 675,184 KiB at measurement.
`affected-unit-1` used 194.5 MiB (203.9 decimal MB) in basetemp and 195.5 MiB (205.0 decimal MB) in scratch at measurement.
`affected-unit-2` used 204.0 MiB (213.9 decimal MB) in basetemp and 213.2 MiB (223.5 decimal MB) in scratch at measurement.

<details>
<summary>Exact pytest commands</summary>

The runner set `R=$PWD/build/sf-a3-fix3`, `TMPDIR=$R/runtime`, `HYPOTHESIS_STORAGE_DIRECTORY=$R/hypothesis`, `PYTHONDONTWRITEBYTECODE=1`, and the lane's `UV_PROJECT_ENVIRONMENT`. For `second_boundary` and `run_counts`, it set `PYTHONPATH=$R:$PWD`. Other runs used `PYTHONPATH=$PWD`.

```sh
# gate-policy-red
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-gate-policy-red -q -rs -vv tests/unit/test_gate_review_family.py::test_claude_gate_submits_sol62 tests/unit/test_gate_review_family.py::test_daemon_preview_resolves_retired_sol tests/unit/test_gate_review_family.py::test_daemon_preview_refuses_unsupported_peer tests/unit/test_gate_review_family.py::test_gate_refuses_missing_sol62_policy_model tests/unit/test_gate_review_family.py::test_recorded_caller_family_refuses_same_family_peer\[claude-fable\] tests/unit/test_gate_review_family.py::test_recorded_gate_family_is_retained_on_peer_job\[claude-astra\] tests/fake/test_gate_end_to_end.py::test_sol62_gate_round_attests_the_requested_model tests/fake/test_gate_end_to_end.py::test_cli_preview_prints_one_policy_alias_note tests/fake/test_gate_end_to_end.py::test_cli_start_prints_one_policy_alias_note tests/unit/test_policy.py::test_retired_sol_alias_resolves_to_sol62_with_note tests/unit/test_policy.py::test_retired_note_can_be_suppressed_for_repeat_evaluations tests/unit/test_scheduler.py::test_retired_sol_alias_resolves_with_note tests/unit/test_scheduler.py::test_claude_review_pin_routes_to_sol62 tests/unit/test_sessions_cli.py::test_the_retired_sol_target_still_parses tests/unit/test_policy_ramp.py::test_shipped_policy_preserves_raw_hash_and_disabled_ramp_defaults tests/unit/test_provider_active_cap.py::test_missing_cap_preserves_bytes_and_hash\[None\]
# boundary-red
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-boundary-red -q -rs -p second_boundary -vvv tests/fake/test_review_family_admission.py::test_i3_i4_durable_decision_rows_and_events_match_a2
# gate-policy-red-serial
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider --timeout=540 --basetemp $TMPDIR/pytest-gate-policy-red-serial -q -rs -vvv tests/unit/test_gate_review_family.py::test_claude_gate_submits_sol62 tests/unit/test_gate_review_family.py::test_daemon_preview_resolves_retired_sol tests/unit/test_gate_review_family.py::test_daemon_preview_refuses_unsupported_peer tests/unit/test_gate_review_family.py::test_gate_refuses_missing_sol62_policy_model tests/unit/test_gate_review_family.py::test_recorded_caller_family_refuses_same_family_peer\[claude-fable\] tests/unit/test_gate_review_family.py::test_recorded_gate_family_is_retained_on_peer_job\[claude-astra\] tests/fake/test_gate_end_to_end.py::test_sol62_gate_round_attests_the_requested_model tests/fake/test_gate_end_to_end.py::test_cli_preview_prints_one_policy_alias_note tests/fake/test_gate_end_to_end.py::test_cli_start_prints_one_policy_alias_note tests/unit/test_policy.py::test_retired_sol_alias_resolves_to_sol62_with_note tests/unit/test_policy.py::test_retired_note_can_be_suppressed_for_repeat_evaluations tests/unit/test_scheduler.py::test_retired_sol_alias_resolves_with_note tests/unit/test_scheduler.py::test_claude_review_pin_routes_to_sol62 tests/unit/test_sessions_cli.py::test_the_retired_sol_target_still_parses tests/unit/test_policy_ramp.py::test_shipped_policy_preserves_raw_hash_and_disabled_ramp_defaults tests/unit/test_provider_active_cap.py::test_missing_cap_preserves_bytes_and_hash\[None\]
# gate-policy-red-isolated
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider --timeout=540 --basetemp $TMPDIR/pytest-gate-policy-red-isolated -q -rs -vvv tests/unit/test_gate_review_family.py::test_claude_gate_submits_sol62 tests/unit/test_gate_review_family.py::test_daemon_preview_resolves_retired_sol tests/unit/test_gate_review_family.py::test_daemon_preview_refuses_unsupported_peer tests/unit/test_gate_review_family.py::test_gate_refuses_missing_sol62_policy_model tests/unit/test_gate_review_family.py::test_recorded_caller_family_refuses_same_family_peer\[claude-fable\] tests/unit/test_gate_review_family.py::test_recorded_gate_family_is_retained_on_peer_job\[claude-astra\] tests/fake/test_gate_end_to_end.py::test_sol62_gate_round_attests_the_requested_model tests/fake/test_gate_end_to_end.py::test_cli_preview_prints_one_policy_alias_note tests/fake/test_gate_end_to_end.py::test_cli_start_prints_one_policy_alias_note tests/unit/test_policy.py::test_retired_sol_alias_resolves_to_sol62_with_note tests/unit/test_policy.py::test_retired_note_can_be_suppressed_for_repeat_evaluations tests/unit/test_scheduler.py::test_retired_sol_alias_resolves_with_note tests/unit/test_scheduler.py::test_claude_review_pin_routes_to_sol62 tests/unit/test_sessions_cli.py::test_the_retired_sol_target_still_parses tests/unit/test_policy_ramp.py::test_shipped_policy_preserves_raw_hash_and_disabled_ramp_defaults tests/unit/test_provider_active_cap.py::test_missing_cap_preserves_bytes_and_hash\[None\]
# gate-policy-green
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-gate-policy-green -q -rs -vv tests/unit/test_gate_review_family.py::test_claude_gate_submits_sol62 tests/unit/test_gate_review_family.py::test_daemon_preview_resolves_retired_sol tests/unit/test_gate_review_family.py::test_daemon_preview_refuses_unsupported_peer tests/unit/test_gate_review_family.py::test_gate_refuses_missing_sol62_policy_model tests/unit/test_gate_review_family.py::test_recorded_caller_family_refuses_same_family_peer\[claude-fable\] tests/unit/test_gate_review_family.py::test_recorded_gate_family_is_retained_on_peer_job\[claude-astra\] tests/fake/test_gate_end_to_end.py::test_sol62_gate_round_attests_the_requested_model tests/fake/test_gate_end_to_end.py::test_cli_preview_prints_one_policy_alias_note tests/fake/test_gate_end_to_end.py::test_cli_start_prints_one_policy_alias_note tests/unit/test_policy.py::test_retired_sol_alias_resolves_to_sol62_with_note tests/unit/test_policy.py::test_retired_note_can_be_suppressed_for_repeat_evaluations tests/unit/test_scheduler.py::test_retired_sol_alias_resolves_with_note tests/unit/test_scheduler.py::test_claude_review_pin_routes_to_sol62 tests/unit/test_sessions_cli.py::test_the_retired_sol_target_still_parses tests/unit/test_policy_ramp.py::test_shipped_policy_preserves_raw_hash_and_disabled_ramp_defaults tests/unit/test_provider_active_cap.py::test_missing_cap_preserves_bytes_and_hash\[None\]
# boundary-green-1
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-boundary-green-1 -q -rs -p second_boundary tests/fake/test_review_family_admission.py::test_i3_i4_durable_decision_rows_and_events_match_a2
# boundary-green-2
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-boundary-green-2 -q -rs -p second_boundary tests/fake/test_review_family_admission.py::test_i3_i4_durable_decision_rows_and_events_match_a2
# boundary-green-3
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-boundary-green-3 -q -rs -p second_boundary tests/fake/test_review_family_admission.py::test_i3_i4_durable_decision_rows_and_events_match_a2
# clock-stability-1
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-clock-stability-1 -q -rs -p no:randomly tests/fake/test_review_family_admission.py::test_i3_i4_durable_decision_rows_and_events_match_a2
# clock-stability-2
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-clock-stability-2 -q -rs -p no:randomly tests/fake/test_review_family_admission.py::test_i3_i4_durable_decision_rows_and_events_match_a2
# clock-stability-3
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-clock-stability-3 -q -rs -p no:randomly tests/fake/test_review_family_admission.py::test_i3_i4_durable_decision_rows_and_events_match_a2
# clock-stability-4
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-clock-stability-4 -q -rs -p no:randomly tests/fake/test_review_family_admission.py::test_i3_i4_durable_decision_rows_and_events_match_a2
# clock-stability-5
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-clock-stability-5 -q -rs -p no:randomly tests/fake/test_review_family_admission.py::test_i3_i4_durable_decision_rows_and_events_match_a2
# review-family-admission
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-review-family-admission -q -rs tests/fake/test_review_family_admission.py
# affected-fake
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-affected-fake -q -rs -p run_counts -m not\ real_lsof\ and\ not\ real_cwd tests/fake/test_admission_route_isolation.py tests/fake/test_daemon_descriptors.py tests/fake/test_gate_end_to_end.py tests/fake/test_launch_env_daemon.py tests/fake/test_launch_env_ignored_hook.py tests/fake/test_reset_credit_incident.py tests/fake/test_review_family_fix_r1_daemon.py tests/fake/test_tier_hold_by_demand.py tests/fake/test_timers_end_to_end.py
# affected-fake-serial
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider --timeout=540 --basetemp $TMPDIR/pytest-affected-fake-serial -q -rs -m not\ real_lsof\ and\ not\ real_cwd tests/fake/test_gate_end_to_end.py::test_changed_plan_during_peer_blocks_and_discards_approval tests/fake/test_gate_end_to_end.py::test_unattested_peer_retries_then_stops_at_policy_cap\[mismatch-astra\] tests/fake/test_gate_end_to_end.py::test_c23_43_unattested_codex_round_counts_and_is_recorded tests/fake/test_gate_end_to_end.py::test_changes_requested_continue_requires_new_approval_or_response tests/fake/test_gate_end_to_end.py::test_plan_cli_round_replays_fixture_through_job_store
# affected-unit-1
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-affected-unit-1 -q -rs -p run_counts -m not\ real_lsof\ and\ not\ real_cwd tests/unit/test_admission_uncapped.py tests/unit/test_app_cutover_daemon.py tests/unit/test_capacity_desktop.py tests/unit/test_claude_cards_wiring.py tests/unit/test_claude_usage.py tests/unit/test_cli.py tests/unit/test_codex_ca_bundle.py tests/unit/test_compat.py tests/unit/test_daemon_fifo_reads.py tests/unit/test_daemon_log_sites.py tests/unit/test_decision_horizon.py tests/unit/test_disk_admission.py tests/unit/test_doctor_ca_bundle.py tests/unit/test_gate_admission.py tests/unit/test_gate_replay.py tests/unit/test_gate_review_family.py tests/unit/test_gate_revision.py tests/unit/test_gate_verdict.py tests/unit/test_guard_trust.py tests/unit/test_hold_note.py tests/unit/test_hold_note_admission.py tests/unit/test_hold_note_review_edges.py tests/unit/test_hold_note_review_r3.py tests/unit/test_importer.py tests/unit/test_launch_env_helpers.py tests/unit/test_launch_env_mutations.py tests/unit/test_launch_env_policy.py tests/unit/test_launch_env_round2.py tests/unit/test_launch_env_round3.py tests/unit/test_launch_env_round8.py tests/unit/test_legacy_hold.py tests/unit/test_operator_hold_closures.py
# affected-unit-2
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-affected-unit-2 -q -rs -p run_counts -m not\ real_lsof\ and\ not\ real_cwd tests/unit/test_operator_notices.py tests/unit/test_picker.py tests/unit/test_policy.py tests/unit/test_policy_disk.py tests/unit/test_policy_ramp.py tests/unit/test_policy_sandbox_resolution.py tests/unit/test_policy_support.py tests/unit/test_policy_tier_preferences.py tests/unit/test_pr124_fixes.py tests/unit/test_pr124_review_r2_fixes.py tests/unit/test_provider_active_cap.py tests/unit/test_reset_credit_demand.py tests/unit/test_reset_credit_properties.py tests/unit/test_review_contract_prompt.py tests/unit/test_review_family_boundary.py tests/unit/test_review_family_fix_r1.py tests/unit/test_review_family_routing.py tests/unit/test_scheduler.py tests/unit/test_scheduler_split.py tests/unit/test_scheduler_tier_preferences.py tests/unit/test_scheduler_weekly.py tests/unit/test_scoped_stream_capacity.py tests/unit/test_sessions_cli.py tests/unit/test_sessions_handoff_excerpts.py tests/unit/test_sessions_mirror_load_gap.py tests/unit/test_sessions_notice_wake.py tests/unit/test_store_contention_queries.py tests/unit/test_timers_daemon.py tests/unit/test_ultracode_effort.py tests/unit/test_weekly_rank_vs_base.py
# gate-service
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-gate-service -q -rs -p run_counts -k not\ merge\ and\ not\ test_changed_pr_after_blocked_preflight_can_receive_fresh_peer_approval tests/unit/test_gate_service.py
# contract-migration
uv --no-cache run --no-sync python -m pytest -p no:cacheprovider -n 4 --timeout=540 --basetemp $TMPDIR/pytest-contract-migration -q -rs tests/fake/test_state_contract.py tests/unit/test_invariants_index.py tests/unit/test_test_module_names.py tests/unit/test_handler_refusals.py tests/unit/test_store_review_family_migration.py
```

</details>

### Skips and harness pruning

- `affected-fake` SKIPPED [1] tests/fake/test_daemon_descriptors.py:194: kern.ipc.somaxconn is 0: no room above the old queue of 64
- `affected-fake` SKIPPED [1] tests/fake/test_gate_end_to_end.py:298: C-5.3 real daemon tests require permitted sysctl/ps inspection: macOS boot identity is unavailable
- `affected-fake` SKIPPED [1] tests/fake/test_timers_end_to_end.py:240: sandbox denies process inspection; bounded turn unit tests cover this seam
- `affected-unit-1` SKIPPED [1] tests/unit/test_daemon_log_sites.py:368: C-5.3 real daemon tests require permitted sysctl/ps inspection: macOS boot identity is unavailable
- `affected-unit-1` SKIPPED [1] tests/unit/test_daemon_log_sites.py:390: C-5.3 real daemon tests require permitted sysctl/ps inspection: macOS boot identity is unavailable
- `affected-unit-1` SKIPPED [3] tests/unit/test_daemon_log_sites.py:326: the interpreter's subfleet is not this checkout's (/Users/maxghenis/subfleet-v2-lanes/review-family/subfleet/__init__.py)
- `affected-unit-1` SKIPPED [1] tests/unit/test_importer.py:1693: C-5.3 real daemon tests require permitted sysctl/ps inspection: macOS boot identity is unavailable
- `affected-unit-1` SKIPPED [1] tests/unit/test_importer.py:2642: C-5.3 real daemon tests require permitted sysctl/ps inspection: macOS boot identity is unavailable
- `affected-unit-1` SKIPPED [2] tests/unit/test_legacy_hold.py:874: C-5.3 real daemon tests require permitted sysctl/ps inspection: macOS boot identity is unavailable
- `affected-unit-1` SKIPPED [4] tests/unit/test_legacy_hold.py:905: C-5.3 real daemon tests require permitted sysctl/ps inspection: macOS boot identity is unavailable

Pytest emitted no kept-root pruning lines.

### Test environment and deviations

- The lane venv ran Python 3.15.2. The first import check resolved `subfleet` to this worktree. The hub added pytest-xdist 3.8.0 and execnet 2.1.2 to `~/subfleet-v2-lanes/review-family/.venv` at 05:10 UTC on 2026-11-28, before dispatch, offline from the uv cache. The base's dev dependencies omit xdist. This job used the existing venv.
- The first functional red and its first serial rerun hit an additional Git discovery issue. The required basetemp is inside the job checkout, so Git found the outer repository from the peer's synthetic review directory and the gate refused its cwd. The first wrapper attempt was lost when zsh initialized PATH. Subsequent gate and affected runs installed a scratch Git wrapper in the runner's PATH. It supplies `GIT_CEILING_DIRECTORIES=$R/runtime` when unset and delegates to the original Git, so a nested test repository is still discoverable. The final isolated serial red supplies the before results above. The forced-clock red used the base clock fixture once.
- In `affected-fake`, five existing CLI cases inherited caller-family markers and refused their Astra peers as same-family. They passed on the serial rerun after the runner cleared the outer session markers. The exact nodes are in the rerun command. The remaining runs used the same marker cleanup.
- The importer search yielded 74 test files and two helpers. The 71 prescribed files ran in batches of 9 fake files, 32 unit files and 30 unit files. The separate gate-service run excluded `test_changed_pr_after_blocked_preflight_can_receive_fresh_peer_approval` in addition to `-k 'not merge'`. That test reaches a merge even though its name omits the word. The xdist deselection counts came from a scratch reporting hook.
- The raw policy fixtures listed under gate peer resolution needed to remove their alias when they removed its Sol target. They retain their no-reviewer and empty-chain assertions.
- The shared object directory refused the remerge diff's temporary object directory. With an isolated scratch object directory and read-only access to the shared objects as alternates, `git show --remerge-diff 670653bf9` exposed the eight conflict entries above.
- The first unit batch's basetemp and scratch readings exceeded 200 MB in decimal units. The second unit batch exceeded 200 MiB in both readings. Counting the measured basetemps once as writes and once as deletions gives approximately 1.38 GB of churn. The runner removed each basetemp before the next run.

### Tests left to CI and scope kept for the hub

- The whole suite, `tests/e2e` and process suites, host-push tests, `tests/unit/test_gate_merge.py`, and the merge cases of `tests/unit/test_gate_service.py` stayed with CI. The reviewed 19-layout store matrix was used from the review. The contract run includes the ordinary focused migration suite.
- `run -m sol` still uses the CLI's Astra remap. See `subfleet/cli.py:92`, its help at `subfleet/cli.py:3915` and C-17.2 at `docs/acceptance-contract.md:700`. `docs/invariants.md:184`, row 109, still describes that remap and already points to C-11.1 for gates and handoff.
- Review section 6 also notes that an operator policy can retain Astra in its hard review chain without a warning. The synthetic Astra-map controls exercise explicit policy compatibility. The hub owns any operator policy change and the PR body.
- The three storage slot numbers, store code, README and the stacked branches are outside this round's changes.

### Cleanup

- The job created `build/` and `build/sf-a3-fix3/` with `bin/`, `logs/`, `link7-objects/`, `runtime/` and `hypothesis/` (including `constants/` and `unicode_data/`). Each of the 20 `runtime/pytest-<run>` basetemps in the table was removed after its run. Git discovery was checked in `runtime/check-ceiling/`, which was removed after the checks. Temporary pytest session homes were removed by the harness.
- The remaining synthetic runtime trees were `runtime/sf-64b4175f-952_xj9z` (2,760 KiB), `runtime/sf-f3bffcea-0wj75_nj` (2,748 KiB) and `runtime/sf-f3bffcea-a_rd5hdx` (2,748 KiB). They were removed with the guarded scratch cleanup.
- Synthetic subprocesses created `subfleet/__pycache__/` (1,332 KiB), `subfleet/adapters/__pycache__/` (16 KiB), `subfleet/conversations/__pycache__/` (264 KiB) and `subfleet/sessions/__pycache__/` (724 KiB) inside the checkout. Those job-created caches were removed.
- The first and last listings of `/private/tmp/sf-*` and `/private/tmp/sf-failed/*` matched, as did the intermediate listings. The short socket fixtures left no new `/private/tmp/sfd-*` or `/private/tmp/sfg-*` directories. There were no new external directories left in place to size. Pre-existing external paths were left to the harness or their owners.

Guarded cleanup completed at 2026-11-28T06:49:29Z.

### Git result

no commit. The sandbox refused the worktree index lock in the shared repository with `Operation not permitted`. The named-path staging steps for tests, production code, contract and docs, and this report all hit that lock.

The requested `git log --format="%H %s" 4d52eca3e4b16069d825fe0af75c5ea801c8e33e..HEAD` returned no entries.

Final HEAD is `4d52eca3e4b16069d825fe0af75c5ea801c8e33e`. The worktree status is below.

```text
 M docs/acceptance-contract.md
 M docs/gates.md
 M docs/reports/2026-11-13-review-family.md
 M subfleet/default_policy.json
 M subfleet/gate/cli.py
 M subfleet/gate/service.py
 M tests/fake/test_gate_end_to_end.py
 M tests/fake/test_review_family_admission.py
 M tests/fake/test_review_family_fix_r1_daemon.py
 M tests/unit/test_gate_review_family.py
 M tests/unit/test_policy.py
 M tests/unit/test_policy_ramp.py
 M tests/unit/test_provider_active_cap.py
 M tests/unit/test_review_family_fix_r1.py
 M tests/unit/test_scheduler.py
 M tests/unit/test_sessions_cli.py
```
