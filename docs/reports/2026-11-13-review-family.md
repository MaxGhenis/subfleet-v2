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

## Fix round 1

Started 2026-11-17 04:10:11 UTC (first command, `date -u`); deadline 07:10:11 UTC. The checkout began clean at detached `48dab4abe`. Every finding in `review-a3-r1.md` is fixed: P1, all four P2s and both P3s. None is left open. Git wrote its lock files this round, so each step is a commit.

| Commit | Step |
| --- | --- |
| `127576d13` | Merge of #314's head `2954abd77` (D-SCHEMA-SLOTS1) |
| `b8d3304ef` | Fix code for every finding, the new tests and the fixtures |
| `a391f84e1` | Test fixes: the unit conftest drops a job's markers; a CLI fake daemon advertises `review.subject.v1` |
| `9916047b1` | Contract, README and `run --help` |
| this commit | This section |

### Merge of 2954abd77

`git merge --no-ff --no-commit 2954abd77` conflicted in nine files. Each was resolved as follows:

- `subfleet/store.py`: slots 11 and 12 are now exactly #314's. Slot 11 is #307's storage schema; slot 12 holds the review fields. Each step is presence-checked, and versions are recorded only after the schema script succeeds, all in one transaction. This PR's `allow_same_family` column moves to a new slot 13 (`REVIEW_FAMILY_MIGRATION`, presence-checked the same way), and `SCHEMA_VERSION` becomes 13. The presence pass now covers slots 11 to 13 on every writable open.
  - I dropped this PR's slot-11 backfill, which marked pending notices `stale`/`before-migration`. #314's slot 11 has none, and the ruling says slot 11 is exactly #314's.
  - `NOTICE_WAKE_PREDATES` is gone.
  - As a result, a store upgraded straight from schema 10 keeps its pending notices with an empty wake state. A store that ran #307 keeps #307's own stale marks.
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

After the fixes, the latest run of each of the 38 test files ran here, in the order the brief sets. The new and changed tests ran first, then the importing files and the store and migration files, then the four required files. All pass: no test fails at head.

### What did not run, and deviations

- **Importing files not run.** `git grep -l` with an import parse finds 354 test modules that import a changed module (`policy`, `scheduler`, `daemon`, `cli`, `protocol`, `refusals`, `store`). This round ran 38 of them, so 316 did not run here; CI runs the whole suite.
- **`tests/unit/test_host_push.py` was not run.** Its fixtures push to local bare repositories, a write-shaped command the brief forbids even behind a stub.
- **Red evidence ran in scratch trees, not this checkout.** The trees were `git archive 127576d13` and `git archive 48dab4abe` (`subfleet`, `tests`, `pyproject.toml`) with the new test files copied in. This shows each test failing on the old code without reverting the checkout.
  - The first red run of the new unit file was a collection error. After a one-line import change it ran per test.
- **The reviewer's probe file (`test_zz_rv_a3_r1_probe.py`) is in the reviewer's workspace.** I wrote `test_p2_sol_first_routing_matrix` instead of rerunning the reviewer's `test_probe_routing_matrix`.
- **`tests/unit/conftest.py` now also drops `SUBFLEET_JOB`, `SUBFLEET_ATTEMPT` and `SUBFLEET_TURN_JOB` from the "outside any session" environment.** These tests run inside a Subfleet job, whose markers would otherwise trip the new consent refusal.
- **The stale-notice backfill is gone** (see the merge above). That follows D-SCHEMA-SLOTS1 and changes what a direct 10 → head upgrade leaves in `notices`.
- **Offline `runs show` (daemon down) shows no caller record.** `same_family_consent` and `caller_family_disagreement` come from the daemon's `show`.
- **Probe commands created cache folders in the checkout.** Early probes ran without `PYTHONDONTWRITEBYTECODE` and created `subfleet/__pycache__`, `subfleet/sessions/__pycache__` and `tests/__pycache__`; the test runs created `.hypothesis` and `uv sync` created `.venv`. All five are listed under cleanup below.
- **Parallel runs.** Several pytest runs ran at the same time, each in the foreground, with at most four workers in total.
