Sandbox hold (D-RO-ADMIT1)

Started 2026-11-12 05:49:40 UTC (`date -u`, first command), baseline b51f08f961e1b1dcfb93347eb080f9ddd9a9260d. Work is confined to the assigned checkout and `/private/tmp/sf-sbh-b1`. Validation and cleanup are complete.

Implementation: sandbox holds use `sandbox-hold:workspace-write`, existing `task-held` wait/event/pin machinery, and existing `tasks` verbs with an exclusive `--sandbox workspace-write` target. Task and sandbox rows compose through the later active end; turns and taskless jobs are exempt. No running attempt or lane is changed. Final validation: 716 passed in the selected-file run, 427 passed in the required contract bundle, and all six sandbox mutants killed; six restored witness tests passed.

Environment deviations: checkout is detached and `feat/sandbox-hold-217` is already checked out elsewhere. Creating an isolated `feat/sandbox-hold-217-sf-sbh-b1` branch was denied by the filesystem at the parent repository ref lock. The default DARWIN_USER_TEMP_DIR denied a top-level scratch folder, so scratch/cache/test temps use `/private/tmp/sf-sbh-b1`. Required `uv sync --group dev --extra api` failed because release/217 defines no `api` extra; `uv sync --group dev` succeeded (9 packages, CPython 3.15.2, pytest 9.1.1, Hypothesis 6.168.1).

Admission and execution read the same resolved value: CLI submission uses explicit `-s` or `policy` (`subfleet/cli.py:932`); resolution is in `policy.resolve_sandbox` (`subfleet/policy.py:40`) and `Daemon.submit` (`subfleet/daemon.py:3076`), before `dataclasses.asdict(args)` persists `jobs.sandbox` (`daemon.py:3305`). The default policy has build=workspace-write and wildcard=read-only, plus a pre-existing review=worktree-write exception (`subfleet/default_policy.json:64`): a review in a committed Git worktree can resolve workspace-write, and a refused uncommitted-copy plan can resolve read-only before insertion (`daemon.py:3161`). Admission reads the stored row (`task_holds.py:242`), never current policy. `_spec` reads `Sandbox(job["sandbox"])` (`daemon.py:2195`), launch passes that spec to the adapter (`daemon.py:9797`, `9837`), and Codex emits its sandbox (`subfleet/adapters/codex.py:582`). Current schema rejects empty/null sandboxes (`subfleet/store_schema.sql:98`); hold readers nevertheless conservatively cover legacy empty values. Production execution semantics are unchanged. Generated invalid-schema rows use an inert fixture spec only after the real hold checks.

CLI as built:

```text
subfleet tasks hold --sandbox workspace-write --until <iso> --note <text> [--if-note-prefix P]
subfleet tasks release --sandbox workspace-write [--if-note-prefix P]
subfleet tasks list
```

Hold/release take either a task or `--sandbox workspace-write`, never both. `list` and online/offline status include both target kinds in `task_holds`; sandbox rows carry `sandbox` instead of `task`. Existing task replies stay unchanged. Help explicitly exempts conversation turns and jobs with no task, and says running attempts are untouched. Conditional note ownership, validation, replacement, `acquired_at` preservation and release rules use the task hold's transactions.

C-11.10 alone was extended by hand, without renumbering. Source anchors and changes:

| Changed path | Current line anchors | Change |
|---|---|---|
| `subfleet/task_holds.py` | `41`, `126`, `175`, `188`, `206`, `213`, `242`, `254` | Separate sandbox lease range; row discriminator; list/count both targets; sandbox release command; composed hold with continuous start; stored sandbox read; reused wait payload. |
| `subfleet/daemon.py` | `284`, `1577`, `1656`, `1728`, `1812`, `1855`, `1862`, `1980`, `1990`, `2047`, `7681` | Read/exclude sandbox range; reuse takes and write-time checks; preserve stale pin pause; target-specific queued-job selection, restore, hold and release; exclude hold leases from capacity census. |
| `subfleet/protocol.py` | `358`, `366` | Optional sandbox target on `TasksArgs`. |
| `subfleet/cli.py` | `569`, `932`, `2426`, `3798`, `3818`, `3833` | Status target rendering; preserve existing submit sandbox source; sandbox tasks payload and output; mutually exclusive task/sandbox CLI and exemption help. |
| `subfleet/offline.py` | `386`, `388` | Include the disjoint sandbox range in offline status. |
| `subfleet/render.py` | `360`, `602` | Render sandbox holds and their release hints through existing status/why machinery. |
| `subfleet/refusals.py` | `317`, `509` | Closed invalid-target refusal; checked sandbox discriminator in conditional ownership refusals. |
| `subfleet/actions.py` | `746`, `749` | Shared write-time held-job check excludes sandbox-held demand and returns `sandbox_held`. |
| `tests/task_hold_mutants.py` | `277` | Update existing request-clock mutant source anchor for the shared target payload. |
| `docs/acceptance-contract.md` | `564` | Handwritten D-RO-ADMIT1 extension within C-11.10; existing clauses retain their numbers. |

No new wait reason or event was introduced: `task-held`, `task.held`, `task.released`, `job.task_held`, `job.task_hold_moved` and `job.task_hold_ended` retain their existing treatment. The production grep covers daemon, CLI, render, MCP and desktop. Desktop preserves the wait string generically; MCP has no separate case. Sandbox payloads use their own discriminator. This also preserves exclusion from idle-admission records/reset-credit demand and shares retry/pin clocks.

Two review findings were fixed: a valid task named `sandbox-hold:workspace-write` cannot collide with the typed internal sandbox index; overlapping holds preserve the earliest continuous pause, including a stale pin writer arriving after the earlier row expires. Neither changes either operator row's acquired time.

Invariants and tests (`tests/fake/test_sandbox_hold_properties.py` unless named otherwise):

| Invariant | Test |
|---|---|
| 1. Held means unplaced: no new attempt for a covered task or sandbox. | `test_s1_held_jobs_never_create_an_attempt` (Hypothesis), plus reservation race tests. |
| 2. Everyone else places as before under the same capacity. | `test_s2_unheld_placements_match_without_the_sandbox_hold` (Hypothesis differential). |
| 3. Running attempts, their jobs and lanes remain unchanged; no signal. | `test_s3_running_attempt_job_and_lane_are_unchanged`. |
| 4. Past-end holds cover nothing; pre-end holds cover every matching job. | `test_s4_hold_membership_obeys_the_clock` (Hypothesis; before/at/after end). |
| 5. Turns and taskless jobs are never held. | `test_s5_turns_and_jobs_without_a_task_are_never_held`. |
| 6. Coverage is exactly task OR sandbox, through the later active end. | `test_s6_task_and_sandbox_holds_compose_to_the_later_end` (Hypothesis). |
| 7. Hold/list/replace/release/list round-trips; in-force replacement keeps acquired_at. | `test_s7_hold_list_replace_release_round_trip`. |

Each generated property uses 25 examples of job sets containing several tasks, read-only/workspace-write/empty sandboxes and dispatch/turn kinds, with generated task and sandbox holds. The differential removes sandbox-covered demand from the reference queue while preserving the unheld population, task holds, capacity and order; leaving that demand in the reference would consume the capacity the hold intentionally frees. Additional interleaving tests cover overlapping releases/replacements, retry deadlines, pin pauses, stale reservation snapshots and reset spends. The two continuity cases open an episode at 10 due at 70, overlap task coverage 0–100 with sandbox coverage 20–200, and delay the writer until 21 or 110: all 60 seconds remain paused and release at 120 restores failure at 180. Exact contract tests pin keys, waits, events, wire refusals and full-store online/offline counts above the 50-job display cap.



Lease reaper check (all named cleanup functions rechecked against the 2026-10-24 report and all lease deletion/release sites):

| Function | Final source line(s) | Selector and sandbox effect |
|---|---|---|
| `Store.release_leases` | `subfleet/store.py:933, 934` | Exact supplied holder, optionally an exact key prefix; ordinary holder releases leave the operator row. |
| `Retirement.commit` | `subfleet/retention_archive.py:974` | Exact `retention:<job>` holder. |
| `Retirement._reclaim` | `subfleet/retention_archive.py:1131` | Delegates to exact `retention:<job>` holder release. |
| `Retirement.rollback` | `subfleet/retention_archive.py:1264` | Exact `retention:<job>` holder. |
| `_Pass._recover` | `subfleet/retention.py:703, 706` | Census restricted to `holder LIKE retention:%`; exact selected key+holder deletion. Separately stale `retire:%`/`resume:%` fences. |
| `_Pass._rollback` | `subfleet/retention.py:1075` | Exact `retention:<job>` holder. |
| `Timers._release` | `subfleet/timers.py:611` | Delegates to `Store.release_leases` for a timer/probe holder. |
| `Daemon._enroll_lane_locked` | `subfleet/daemon.py:1412` | Enrollment holder only. |
| `Daemon._release_task` | `subfleet/daemon.py:2084` | **Intentional operator release:** exact selected task or sandbox key; releasing one target does not delete the other. |
| `Daemon.submit` | `subfleet/daemon.py:3468` | Exact `retire:<source>` fence key and `resume:<request>` holder. |
| `Daemon._fence_resume` | `subfleet/daemon.py:3600` | `lease_key LIKE retire:% AND holder LIKE resume:%`. |
| `Daemon._release_worktree_hold` | `subfleet/daemon.py:5581` | Exact `cwd:<job>` holder. |
| `Daemon._keep_for_cwd` | `subfleet/daemon.py:5609` | `worktree:%` keys held by the ending job or attempt. |
| `Daemon._release_ending` | `subfleet/daemon.py:5643` | Ending job/attempt holders; optionally retains worktree keys. |
| `Daemon._recover_then_start_timers` | `subfleet/daemon.py:6004, 6008` | `retire:%`/`resume:%` fence cleanup, then `probe:timer:%` holder census and release. |
| `Daemon._finish_probe` | `subfleet/daemon.py:7061, 7084` | Exact durable probe holder. |
| `Daemon._skip_revive` | `subfleet/daemon.py:9613` | Exact revive job holder. |
| `Daemon._quarantine` | `subfleet/daemon.py:11305` | `lane:%` keys held by the quarantined attempt. |
| `Daemon._resolve_quarantine` | `subfleet/daemon.py:11336` | Quarantined job/attempt holders; optionally retains worktree keys. |
| `Daemon._finalize` | `subfleet/daemon.py:12029, 12045` | Attempt holder restricted to `lane:%`, then job/attempt holders with optional worktree retention. |
| `Daemon._export_locked` | `subfleet/daemon.py:12224` | Exporting job/attempt holders with optional worktree retention. |
| `GateService._consume` | `subfleet/gate/service.py:417` | Exact gate round lease key and holder. |

`Daemon._recover_probes` begins from `holder LIKE 'probe:%'` (`daemon.py:7092`). No general expiry reaper exists. Ordinary holders differ from `operator: <note>`, and component key selectors exclude `sandbox-hold:`. The key `sandbox-hold:workspace-write` sorts before `task-hold:` and cannot enter `[task-hold:, task-hold;)`. Tests exercise future and expired rows through the real Store, retention, startup, probe, timer and ending-job cleanup paths; both remain stored unchanged. Admission's capacity lease census excludes both ranges.

Validation command convention: the initial `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"` was replaced by the permitted fallback. Pytest commands used `TMPDIR=/private/tmp/sf-sbh-b1`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, `uv --cache-dir "$UV_CACHE_DIR" run --no-sync --with 'pytest-xdist>=3.6' pytest`, and a new `--basetemp "$TMPDIR/pytest-<run>"`. Later commands also set `UV_PROJECT_ENVIRONMENT=$TMPDIR/venv`, `PYTHONPATH=$TMPDIR:$PWD`, and `-p pytest_scratch`. The scratch plugin redirects explicit `/tmp` allocations and the framework's default failed-state census into this job's scratch; it does not change admission. It records allocated directories in a manifest. The original workspace `.venv` was moved under scratch and deleted with a guarded `shutil.rmtree`; the replacement interpreter/cache were also created under scratch and removed at cleanup.

Commands and results (logs are in `2026-11-12-sandbox-hold-data/`):

| Command / run | Result |
|---|---|
| First command: `date -u`, `pwd`, `git status --short`, `git branch --show-current`, scoped `rg --files`. | Start 05:49:40 UTC; assigned checkout; clean detached baseline. |
| `git log -1 --oneline`, `git branch --list feat/sandbox-hold-217`, source `rg`/`sed`/`cat`, Python AST inventory. | Verified baseline, branch occupied elsewhere, sandbox provenance and every reaper; source audit records searches. Missing exploratory paths/globs returned 1/2 and were retried with valid paths. |
| `git switch -c feat/sandbox-hold-217-sf-sbh-b1`. | Denied parent ref lock; no branch changed. |
| `getconf DARWIN_USER_TEMP_DIR`; `mkdir -p` under it, then `/private/tmp/sf-sbh-b1`. | Default top-level child denied; fallback succeeded. |
| `uv sync --group dev --extra api`; fallback `uv sync --group dev`. | `api` extra absent (exit 2); fallback succeeded, 9 packages. Repeated sync verified packages, then installed 9 into the scratch interpreter. |
| `pytest -q tests/unit/test_sandbox_hold_cli.py` (`cli-1.log`). | 18 passed, 21.57 s. |
| `pytest -q tests/unit/test_sandbox_hold_reapers.py` (`reapers-1.log`). | 26 passed, 5.29 s. |
| `pytest -q tests/fake/test_sandbox_hold_properties.py` (`properties-1.log`). | Initially 7 failed / 11 passed, 337.12 s: fixture lacked measured quota for modeled writable jobs. |
| `pytest -q tests/unit/test_sandbox_hold_contract.py` (`contract-1.log`). | Initial 24 passed, 11.59 s. |
| `pytest -q tests/fake/test_sandbox_hold_continuity.py` (`continuity-1.log`). | 2 passed, 7.68 s. |
| `pytest -q -x --hypothesis-show-statistics tests/fake/test_sandbox_hold_properties.py` (`properties-2.log`). | 18 passed, 98.69 s; four properties each 25 generated passing examples. Fixture now supplies measured quotas and refuses probe guardians. |
| `pytest -q tests/fake/test_sandbox_hold_interleavings.py` (`interleavings-1.log`). | Initially 3 failed / 10 passed, 8.26 s: fixture finalization required unrelated writable salvage setup. |
| Same interleavings file with `-k` selecting the three failures (`interleavings-fixes-targeted.log`), then the complete file (`interleavings-fixed-full.log`). | 3 passed / 10 deselected, 13.04 s; then 13 passed, 9.47 s. Fixture finalizes its read-only attempt before modeling the stored writable retry; hold/reset checks remain real. |
| `pytest -q tests/unit/test_sandbox_hold_contract.py` (`contract-2.log`). | 28 passed, 21.50 s, including four actual submission/row/manifest/spec cases. |
| Scoped `rg -l 'task_holds|task-held|tasks hold|job_held|wait_hold' tests --glob 'test_*.py'`, checked against a Python file inventory. | 22 matching files; added the related CAS socket integration file, 23 total, listed in `selected-tests.txt`. |
| `python3 .../run_selected.py`: one `pytest -q -n 4` run of those 23 files (`selected-1.log`). | 713 passed / 3 failed / 6 skipped / 1 xfailed, 340.67 s. Failures were old mutant injection anchors/signatures; adjusted the request-clock anchor and preserved the original positional task release-hint call. |
| `SF_SBH_RUN=2 python3 .../run_selected.py`: the same 23 files together, `-n 4` (`selected-2.log`). | 716 passed / 6 skipped / 1 xfailed, 190.59 s. |

The six skipped cases are the opt-in legacy `TASK_HOLD_MUTANT_MATRIX` property matrix. The strict expected failure is existing `test_c6_10_a_wait_of_another_kind_does_not_record_the_same_verdict_again` (`tests/fake/test_admission_properties.py:614`), unrelated to sandbox holds. The whole suite and that optional matrix were not run, as requested.

The requested next command, `pytest -q -n 4 tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py`, passed **427 tests in 54.40 s** (`contract-bundle-1.log`). C-11.10's manual edit passes the invariant index.

After that bundle, `rg -n` over test modules audited all shared wait/event names plus `sandbox-hold:`, `sandbox_held`, `tasks-target` and `task-hold-condition`: 149 references are saved in `all-exact-contract-references.txt`. The new exact-contract tests extend the pinned payloads; existing task expectations stay unchanged. The original request-clock mutant fixture's source anchor was updated.

`python3 .../mutants.py` applied each source mutant separately and restored every target from pristine scratch copies in `finally`, checking bytes after every restore. All six were killed. The exact commands, assertions and restored SHA-256 hashes are in `mutation-results.json`; individual `mutant-*.log` files preserve failures. `git checkout` was unnecessary.

| Mutant (applied alone) | Killing test in `test_sandbox_hold_properties.py` | Result |
|---|---|---|
| 1. Read policy default instead of stored sandbox | `test_sandbox_hold_reads_the_job_row_instead_of_the_task_default` | Killed: expected assertion, exit 1. |
| 2. Treat empty sandbox as read-only | `test_empty_sandbox_is_conservatively_held` | Killed: expected assertion, exit 1. |
| 3. Hold a conversation turn | `test_s5_turns_and_jobs_without_a_task_are_never_held` | Killed: expected assertion, exit 1. |
| 4. Keep an expired hold in force | `test_expired_sandbox_hold_stops_nothing_without_being_reaped` | Killed: expected assertion, exit 1. |
| 5. Sandbox release deletes task holds too | `test_sandbox_release_keeps_an_independent_task_hold` | Killed: expected assertion, exit 1. |
| 6. Lease reaper deletes sandbox rows | `test_sandbox_hold_survives_lease_reapers_and_is_outside_task_range` | Killed: expected assertion, exit 1. |

A final `pytest -q -n 4` run of all six named witness tests passed **6 tests in 8.22 s** (`post-mutation-controls.log`). An independent SHA-256 check confirmed pristine `task_holds.py`, `daemon.py` and `store.py`. `git --no-optional-locks -c core.fsmonitor=false diff --check`, `python3 -m compileall -q` on changed production modules, and `python3 -m py_compile` on production/test/report helpers passed.

Remaining command inventory: `rg`, `sed`, `cat`, `tail`, `wc`, `pwd`, `date -u` and Python source/AST/file inventories inspected only this checkout, repository objects or job scratch; successful reads and report writes exited 0. Empty search matches or nonexistent exploratory paths/globs returned 1/2, then valid-path reads succeeded. Two report-table extraction attempts raised `ValueError`/`StopIteration` without changing files and were corrected. Git status/diff reads sometimes stalled; completed reads or interrupted read sessions are recorded in the source audit. Validation logs, command scripts and the exact selected-file command are preserved beside this report.

Deviations and cleanup: the required `api` extra does not exist; fallback sync used the declared dev group. The brief's simplified policy default omits existing conditional writable reviews; the implementation follows the stored resolved sandbox. Current schema excludes empty values, so generated legacy rows temporarily bypass the fixture CHECK constraint; the conservative hold path is real, and inert legacy launch normalization is fixture-only. The initial generated fixture entered fake admission probes before measured quota was supplied; the corrected fixture forbids guardian spawning. Initial pytest startup used the repository's default `/tmp/sf-failed` census; later runs redirected it and explicit `/tmp` allocations into job scratch. The six opt-in matrix cases and whole CI suite were not run. No live store, credential, lane enrollment, caller working-tree mutation, history rewrite or push was requested or performed.

The initial interpreter at `/Users/maxghenis/.subfleet/worktrees/20261111-122607-sbh-build-r1s/.venv` was moved to `/private/tmp/sf-sbh-b1/original-venv` and deleted with a guard asserting `sf-sbh-b1` in the path. Subsequent interpreter, uv overlays/cache, pytest bases, safe homes, fixture directories, mutant pristine copies and allocation plugin were created under `/private/tmp/sf-sbh-b1`; `temp-directories.jsonl` records 1,824 allocated directories, all inside that scratch. 12 generated worktree Python/pytest/Hypothesis cache directories were likewise recorded in `worktree-cache-cleanup.json`, relocated under scratch, and removed with the marker guard. The complete scratch directory was removed after copying evidence with a guarded `shutil.rmtree` asserting the marker and exact parent. No scratch process is left running.

Finished 2026-11-12 06:31:33 UTC, within the four-hour limit. Commit attempts use named files only. Creating an isolated branch ref was denied; staging and committing the implementation and the clock/mutant fix were denied at the checkout's `index.lock`. Final named-file staging and commit were also denied at `index.lock` (both exit 128); logs and exact argument lists are saved in `commit-final-*`. No commits could be created. The checkout remains detached at the baseline; the hub can collect all changed and new files.

Head built: b51f08f961e1b1dcfb93347eb080f9ddd9a9260d


## CI fix r1

Started **2026-11-13 12:49:02 UTC**, from `date -u` in the first command, in the assigned clean detached checkout at `ea28630b51603439fa4a402f6390916ef5f0dbf6`. Deadline: 14:49:02 UTC. All eleven original failures are repaired in test stand-ins; production is unchanged.

The three failures were fixture contract mismatches:

- `test_host_push_build_budget.py`: `Store.execute` returned the entire job dictionary for `SELECT kind, task, sandbox`, so `job_held` raised `KeyError: 0`. It now returns the selected positional tuple, or None, while lease reads still return the correct empty rows.
- `test_host_push_store_completion_pr183.py`: `Rows.jobs` lacked `kind`, `task`, and `sandbox`. The primary `OperationalError: no such column: kind` entered permanent-error handling, whose `_job` exposed the secondary missing-`get_job` error. The miniature SQLite schema now persists those columns, and `job()` reads their actual values.
- `test_host_push_store_interleavings.py`: `BusyRows` inherits that incomplete schema. The same SQL error entered its harness's failure handler before any transient retry counter existed, causing secondary `KeyError: '0'`. The shared `Rows` repair fixes it without changing its fault schedule or failure assertions.

Production is correct: `Daemon` constructs the real `Store` (`daemon.py:1012`), which uses `sqlite3.Row` (`store.py:323`) and yields its actual connection from `transaction` (`store.py:434`). The selected positional read inside that transaction sees the stored job fields and covering leases together. No production code changed. Repairs are at `test_host_push_build_budget.py:190,206` and `test_host_push_store_completion_pr183.py:79,86,103`. Both harnesses now use task-bearing build jobs, exercising the lease lookup too. All **176 original assertions, five settings calls, and five generated-input decorators/strategy bounds** are unchanged.

Added `tests/unit/test_host_push_sandbox_hold.py::test_push_workspace_preparation_reads_sandbox_hold_in_real_store`: one parameterized test, two cases, using a file-backed temporary Store, the production readers, and a deterministic pending preparation Future. A workspace-write sandbox lease committed after the job snapshot produces the durable `task-held` wait/deadline and exact hold event; no hold produces the `workspace` preparation wait/event. Both cases leave no attempt or worktree. This passed before the stand-ins were repaired.

The caller audit includes hold-move, hold-take, hold-release, admission/pin/probe/error writers and reset-credit demand. Additional reachable wrappers are in `host_push_store_interleavings`, `host_push_storage_recovery`, `host_push_storage_retry_budget`, fake `task_hold_cas`, `task_hold_clocks`, `sandbox_hold_continuity`, `probe_turn`, `probe_cancel`, `probe_deadline`, unit `reset_credit_claim_timing` and `timers_reset_credits`; all delegate to real SQLite except the repaired three stand-ins. The 55-file run includes all of them, all 45 filename matches under unit/fake, and all 23 hold-reference files. The exact inventory and excluded unrelated census/conversation/transport stand-ins are recorded in the evidence audit.

Every pytest invocation used scratch `/private/tmp/sf-sbh-c1`, its own new basetemp, scratch interpreter/cache/Hypothesis/bytecode paths, and `uv --cache-dir /private/tmp/sf-sbh-c1/uv-cache run --no-sync --with 'pytest-xdist>=3.6' pytest -q -p no:cacheprovider -p pytest_scratch`. CPython was **3.12.14**, pytest 9.1.1, Hypothesis 6.168.1. `run_tests.py` records fully expanded commands, environments, exit statuses and elapsed times; the following are its label and trailing arguments.

| Command | Result |
|---|---|
| `repro-build-budget tests/unit/test_host_push_build_budget.py` | Exit 1: 9 failed, 6 passed, 213.16 s. |
| `repro-store-completion tests/unit/test_host_push_store_completion_pr183.py` | Exit 1: 1 failed, 1 passed, 159.33 s. |
| `repro-store-interleavings tests/unit/test_host_push_store_interleavings.py` | Exit 1: 1 failed, 25 passed, 563.24 s. |
| `real-store-witness tests/unit/test_host_push_sandbox_hold.py` | Exit 0: 2 passed, 13.00 s. |
| `fixed-build-budget tests/unit/test_host_push_build_budget.py` | Exit 0: 15 passed, 119.57 s. |
| `fixed-store-completion tests/unit/test_host_push_store_completion_pr183.py` | Exit 0: 2 passed, 55.53 s. |
| `fixed-busy-generated tests/unit/test_host_push_store_interleavings.py::test_busy_store_completion_cancel_pass_restart_interleavings` | Exit 0: 1 passed, 43.69 s. |
| `selected -n 4 @selected-tests.txt --durations=15` | Exit 1: 1 failed, 1,310 passed, 7 skipped, 1 xfailed, 3119.97 s. |
| `retry-store-interleavings tests/unit/test_host_push_store_interleavings.py --durations=10` | Interrupted for deadline: 23 passing progress markers of 26 cases; wrapper exit 130, no final pytest summary. |
| `contract-bundle -n 3 tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py` | Exit 0: 427 passed, 1133.24 s. |

The additional broad-run failure was `test_restart_finds_unrecorded_completion_after_busy_ownership[False]`: `finished(world)` could not complete its initial submit because the quarantine's `git init` exceeded the unchanged 60-second production cap. The traceback precedes preparation and the hold query. The serial repeat completed its first 23 cases, including both restart variants, before interruption during the generated admission property to preserve time for cleanup/reporting. No code, assertion or timeout changed. Its incomplete rerun is not a complete passing-file result. The broad run passed all eleven originally failing cases, including that generated property. The seven skips are the six opt-in legacy mutant-matrix cases and the opt-in local-repository benchmark; the strict expected failure is the existing admission-properties case.

The baseline files ran one at a time and finished before fixture edits: **all eleven CI failures reproduced**, with 32 other tests passing. Focused post-fix processes used three isolated workers in total; the broader run used four. The serial repeat and three-worker contract bundle ran together, never exceeding four workers.

Setup and command inventory: default Darwin scratch creation was denied; `/private/tmp/sf-sbh-c1` succeeded. Required `uv sync --group dev --extra api` refused the absent `api` extra (exit 2); fallback dev sync installed nine packages. Python was then explicitly pinned to 3.12 and a final sequential sync checked all nine packages before tests. Scoped `cat`, `sed`, `rg`, Python AST/file inventories, Git status/log/diff/head reads and progress reads succeeded, except absent AGENTS.md/no-match exploratory searches (exit 1). `git diff --check` passed. Seven Python files compiled successfully. The AST assertion/bound audit and source hashes confirm production and the interleavings harness stayed unchanged and the earlier report text was preserved. Detailed read ranges, every expanded test command and counts are retained in the evidence inventory.

Evidence is collected under the existing `docs/reports/2026-11-12-sandbox-hold-data/` directory with the `ci-r1-` prefix: `root-commands.md`, `standin-audit.md`, selected-file inventories, expanded `*.command.json` records, logs, assertion/source checks and cleanup audit. Commands recorded before collection retain their actual original evidence paths.

Deviations: the required `api` extra is absent; the permitted dev-group fallback was used. Two setup syncs overlapped, then a sequential Python-3.12 sync verified all nine packages before testing. An audit artifact read stalled and was interrupted (exit 130); missing exploratory paths returned 1/2. The serial repeat was interrupted for the hard deadline after 23 passing markers. A subsequent signal attempt scoped to this job's exact retry basetemp (`pkill -INT -f /private/tmp/sf-sbh-c1/pytest-retry-store-interleaving[s]`) failed with exit 3 because process-list access/sysmond is unavailable; no process list was returned. No live application state was accessed. Python 3.14, remote CI, the whole suite and the opt-in matrix/benchmark were not rerun.

Commit attempts: isolated branch creation was denied at the parent ref lock. Named-file witness and fixture staging were denied at the assigned checkout's `index.lock` (exit 128); no commit could be made. Final named-file staging also failed at `index.lock` (exit 128), recorded in `ci-r1-commit-final.json`. No history rewrite or push was performed.

Cleanup command `python3 docs/reports/2026-11-12-sandbox-hold-ci-r1-data/cleanup.py` copied and SHA-256 verified 44 evidence files into the existing report-data directory. It recorded 3760 fixture-directory allocations and 6 external allocation roots, with 0 external roots requiring final deletion. The temporary assembly folder `docs/reports/2026-11-12-sandbox-hold-ci-r1-data` was moved under scratch and removed with guarded `shutil.rmtree` asserting `sf-sbh-c1`. No worktree cache roots required removal. The plugin recorded six earlier guarded external-directory deletions. The scratch-tree deletion was interrupted near the hard deadline; `/private/tmp/sf-sbh-c1` remains partially deleted. `ci-r1-cleanup.json` records the interrupted cleanup and every original path. This incomplete scratch deletion is a deviation. External directory paths recorded and removed earlier: `/Users/maxghenis/.subfleet/worktrees/20261113-071657-sbh-fix-ci-r1/sf-sbh-c1-sfwork-agr3i_7x`; `/Users/maxghenis/.subfleet/worktrees/20261113-071657-sbh-fix-ci-r1/sf-sbh-c1-sfwork-d3tutwru`; `/Users/maxghenis/.subfleet/worktrees/20261113-071657-sbh-fix-ci-r1/sf-sbh-c1-sfwork-gfbkj1b9`; `/Users/maxghenis/.subfleet/worktrees/20261113-071657-sbh-fix-ci-r1/sf-sbh-c1-sfwork-vuov_cvk`; `/Users/maxghenis/.subfleet/worktrees/20261113-071657-sbh-fix-ci-r1/sf-sbh-c1-sfwork-xc5ng8ol`; `/Users/maxghenis/.subfleet/worktrees/20261113-071657-sbh-fix-ci-r1/sf-sbh-c1-sfwork-xyi6u8qb`.

Final reads/review of the three changed/new test sources found no material issue (exit 0). Git status showed only the requested report, two repaired fixture files, the new regression file and CI-fix evidence. `git --no-optional-locks -c core.fsmonitor=false diff --check` passed; `rev-parse HEAD` confirmed the original head. Final report/evidence updates and progress reads succeeded. No test bound, assertion, production timeout or production code changed.

Tests and cleanup were stopped before the deadline. The final report write completed at **2026-11-13 14:49:13 UTC**, eleven seconds after the two-hour limit; this is a deviation. No commit was created because every named-file staging attempt was denied.

Head built: ea28630b51603439fa4a402f6390916ef5f0dbf6
