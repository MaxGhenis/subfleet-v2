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


## Fix r1

Started **2026-11-15 02:29:42 UTC** (`date -u`, first command), in a detached checkout of `feat/sandbox-hold-217` at c57fb15e3, the head review r1 read. The deadline was 06:00 UTC. Scratch was `$(getconf DARWIN_USER_TEMP_DIR)sf-sbh-f1`. Evidence is beside this report, prefixed `fix-r1-`: the runner and mutant harness, `runs.jsonl`, `mutants.jsonl`, probe scripts and outputs, and every log.

Both P2s are fixed. All three P3s are dealt with: the version mismatch, the push race and the PR body's overclaim. Six of seven mutant runs were killed; the one survivor, m2, is equivalent (shown below).

### Step 0: merge release/217

`origin/release/217` was at 82d3dd819 as the brief named. The merge base was 85769b826, and 27 commits came in, among them #304: conditional task holds as `hold-if`/`release-if`, capability v2, and untrimmed prefixes refused. Conflicts were where the brief said:

- **`docs/acceptance-contract.md`:** resolved with `python3 /Users/maxghenis/reviews/subfleet-hub/host-runs/merge_contract.py HEAD origin/release/217`. Output: "merged against 85769b826: 257 clauses, none duplicated" (exit 0), no conflict markers left.
  - C-11.10 changed on both sides, and the script word-merged it. A word diff shows merged equals ours plus exactly theirs' 13 word edits.
  - No clause id is duplicated (`uniq -d` on the clause ids is empty).
  - The change log has no new gap: it holds 69 blank lines merged and 69 in release/217 (68 ours). The blank line after the top entry is release/217's own.
- **`subfleet/cli.py`, hunk 1** (`cmd_tasks` payload). Ours dropped `sandbox` and re-added it for a sandbox target. Theirs sends a conditional call as `<action>-if` with `if_note_prefix`. Both are kept: sandbox first, then the conditional action. The daemon's `_tasks` routes `hold-if`/`release-if` to `_hold_task`/`_release_task`, whose `_hold_target` reads `sandbox`.
- **`subfleet/cli.py`, hunk 2** (`tasks` description). Ours had the sandbox and exemption sentences. Theirs had "Prefixes must have no surrounding spaces" and v2. All three are kept.
- **`subfleet/cli.py`, hunk 3** (`tasks release` parser). Ours had the task-or-`--sandbox` group. Theirs had the new prefix help ("no surrounding spaces"). Both are kept.
- **`subfleet/refusals.py`, hunk 4** (`render`, `task-hold-condition`). Ours names `sandbox X` or `task X`. Theirs omits "since" when it is null. Both are kept.
- **Semantic fallout:** two of our tests pinned rules #304 changed. Both are updated in the merge commit, and no other change is in it.
  - `test_sandbox_condition_reuses_note_prefix_capability` sent `" disk: "`, which is now refused, and expected a capabilities preflight, which #304 removed. It now sends `disk:` and expects `hold-if`/`release-if`.
  - `test_sandbox_condition_wire_rejects_invalid_or_mixed_discriminators` expected no reason for an unshowable row. #304 keeps `task-hold-condition` and drops only `details`.
- **Tests before going on:**
  - `merge-1`: `test_handler_refusals.py`, `test_sandbox_hold_cli.py`, `test_task_hold_cas_cli.py`, `test_task_hold_cas_wire.py`, `test_task_holds.py`, `test_sandbox_hold_contract.py`, `test_task_hold_cas_integration.py` and `test_invariants_index.py`. Result: 592 passed, 9 failed (the two tests above).
  - `merge-2`, after the updates: 46 passed.
- **Commit:** abbc77b44, with parents c57fb15e3 and 82d3dd819.

### The findings

**1. P2: change-log line.** Two lines now sit at the top of "Changes in version 3" (`docs/acceptance-contract.md:9-10`), followed directly by the existing 2026-11-11 entry with no blank line inside the list:
- the 2026-11-12 amendment;
- fix r1's 2026-11-14 changes.

`grep -c D-RO-ADMIT1` went from 1 to 3. Commit: 4cd213782.

**2. P2: a release with no row.** `subfleet/daemon.py:2087-2093` now returns `{"released", "was_held": False, "woken": 0, "task_holds"}` before reading any job when its target has no row. That covers sandbox and task, plain and conditional. The docstring is at 2063-2067.
- **Which base behaviour stays:** C-11.10 lets a release wake stale jobs only as "a release of a hold already past its end", which has a row. A release with no row "changes nothing and records nothing".
  - The base's task release with no row woke stale jobs too, and the clause doesn't say so. So that is fixed as well: neither kind of release with no row wakes a job that a stale hold of either kind kept waiting.
  - A release whose row exists and has expired behaves as before.
- **Unreadable rows:** the check is `row is None`, not `ended is None`. So an unconditional release still deletes a row whose end can't be read (`hold_of` gives None), as before.
- **Liveness:** such a job waits `task-held` until its hold's end, and the next pass gives it back there.
- **Contract:** the C-11.10 sentence now reads "A release with no row of its own target …".
- **Tests** (`tests/fake/test_sandbox_hold_release_rows.py`):
  - `test_a_release_with_no_row_of_its_target_changes_and_records_nothing[p5-sandbox-release]` is r1's p5. Build is held until 50, the clock is at 60 with no pass, and a sandbox release runs with no sandbox row. The reply has `woken` 0, and the store image (leases, jobs, attempts, events, closures) is unchanged. A pass then ends the hold at 50 (`job.task_hold_ended`, `by: end`) and places the job once.
  - `[task-release]` is the mirror: a stale sandbox hold, and a task release with no task row.
  - The control, `test_control_a_release_whose_target_has_a_row_still_wakes_the_stale_job[both]`: the released target's row exists and ended at 40, while the job was held until 50. The result is `woken` 1, `job.task_hold_ended` with `ended_at` 50, and `task.released` with `until` 40. The released row is gone and the other row stays.
  - `test_a_release_with_no_row_and_no_held_job_is_a_no_op_reply`.
- **Left for round 2:** when both rows exist and both have expired, a release of either kind wakes the job from its own clock, the later end. Its `job.task_hold_ended` names the kind of its last take, not the released kind. This follows C-11.10's composition and its rule for releasing a stale hold, and the control pins it. I left it unchanged.
- Commit: 4e841f6c3.

**3. P3: mixed CLI and daemon versions.**
- **The capability:** `protocol.TASKS_SANDBOX_CAPABILITY = "tasks.sandbox.v1"` (`subfleet/protocol.py:55-58`). The daemon lists it (`subfleet/conversations/service.py:105-107`).
- **The CLI check** (`subfleet/cli.py:2471-2483`). Before sending a sandbox target, the CLI asks for `capabilities`. If the capability is missing, it refuses and sends no `tasks` request:
  - prose: `task-hold-capability: tasks <action> --sandbox needs daemon capability \`tasks.sandbox.v1\`; this daemon does not support sandbox holds`, exit 6 (`Exit.CLI_TOO_OLD`), with the restart as the fix;
  - with `--json`: `{"refused":"task-hold-capability","capability":"tasks.sandbox.v1"}`.
- **Why that refusal and exit:** #304 already uses this refusal and exit 6 for a daemon missing a tasks capability, and C-11.10 says 69 can't name one.
- **A task target** keeps its wire shape and gets no preflight.
- **Why a preflight is safe here, though #304 has none for the condition:** a daemon swapped for an older one after the check still writes nothing (p6's daemon half, below).
- **Help text:** the `tasks`, `hold` and `release` help now say "exit 6: daemon missing tasks.if_note_prefix.v2, or tasks.sandbox.v1 for --sandbox" (`cli.py:3827-3828, 3840-3841, 3857-3858`).
- **p6 tests** (`tests/unit/test_sandbox_hold_versions.py`) run on the short `/tmp` socket root in `tests/unit/conftest.py`, so the `AF_UNIX path too long` failure doesn't recur.
  - The main test covers three older-daemon answers (release/217 before #309 listing v1 and v2; no tasks capability; no `capabilities` op), each with hold or release, plain or conditional, and `--json` or prose: 24 cases. Each asserts exit 6, the exact JSON or prose, and `server.ops() == ["capabilities"]`.
  - Further tests:
    - a task target is unchanged (3 cases);
    - a new CLI and daemon round-trip through the socket with the real advertisement (2);
    - the daemon advertises the capability (1);
    - the old CLI's release hint releases nothing (1).
- **p6, daemon half** (probe, `fix-r1-p6.json`): the release/217 daemon at 82d3dd819, with a task hold row present, given each payload the new CLI sends (`hold`, `release`, `hold-if`, `release-if` with `sandbox`). Each was refused `tasks hold|release: unknown task None`, exit 2, `unknown-task`, with the store unchanged. This proves r1's UNPROVEN reading.
- **p7, an old CLI against a new daemon** (probe, `fix-r1-p7.json`: the 82d3dd819 CLI's renderers fed the new daemon's sandbox reply and wait):
  - `tasks list` shows `None until <end>: disk brake`, and status shows `None until <end> (1 waiting): disk brake`;
  - `why` says "the operator holds ? jobs … `subfleet tasks release -- '?'`";
  - its parser rejects `--sandbox` (argparse exit 2).

  The new daemon refuses `release -- '?'` as `unknown-task` with nothing changed (test above). C-11.10 now says all of this.

  Correction (2026-11-26, issue #327): that holds for this fixture's policy, which has no task `?`. Policy validation accepts a task named `?`; the hint then releases that task's hold, never the sandbox hold. See `docs/reports/2026-11-26-release-hint-doc.md`.
- Commits: 7918a8aad and 4cd213782.

**4. P3: the push-worktree race.** The build is now queued inside the `job.workspace_preparing` transaction, after `_task_held_at_write` finds no hold (`subfleet/daemon.py:6399-6424`).
- **Paths covered:** a job's first look, and a later pass offering a build again to a job already waiting `workspace`. That second path opened no transaction before, so it never re-read the hold.
- **Holds that commit later:** a hold committing after that transaction finds the build in flight, which C-11.10 and C-4.1 allow.
- **Why it is small and safe:** it is about a dozen lines.
  - The worker's first store read goes to a reader connection (`Store.one`, `_reads_elsewhere`), so queuing it under the writer doesn't block the build.
  - A busy store at BEGIN now means no build this pass. `_push_preparation_offered` is set as before, so the next pass retries.
  - A fault after the submit leaves the same state as before.
- **Test updates:**
  - `tests/unit/test_host_push_sandbox_hold.py`: line 52's `len(submitted) == 1` is now `(0 if held else 1)`, and a re-offer case was added (4 cases).
  - `test_host_push_store_interleavings.py::test_permanent_preparing_error_keeps_running_build_slot_until_cleanup` pinned the old order: its hook opened the preparing transaction only after the worker started, so under the new order it waited 30 s and failed (`push-3`). The hook now waits for the worker at the submit inside that transaction, so the UPDATE still faults with the build running. No assertion changed.
  - All 22 `test_host_push*` files: 402 passed, 1 skipped.
- **Contract:** C-11.10 now says a host-push checkout is queued only after that read.
- Commit: b9d28a3af.

**5. P3: the PR body overclaims.** The command's description and the `hold`/`release` help were already accurate. The `--sandbox` argument help said "hold jobs with this stored sandbox" and now says "hold detached jobs that have a task and this stored sandbox" (`cli.py:3845-3846`; commits 8ce4c4de7 and 1535cebbb). r1's p4 is now permanent: `test_p4_the_sandbox_hold_commit_leaves_taskless_jobs_and_turns_queued`.

### Corrected PR opening

> D-RO-ADMIT1. Adds a sandbox hold beside C-11.10's task holds. While it is in force, no new attempt starts for a detached job that has a task and a stored workspace-write sandbox, and read-only work keeps placing. As with task holds, a job submitted without `--task` (for example `subfleet run -m astra -s workspace-write`) and a conversation turn are never held, so a sandbox hold doesn't stop every write during a disk brake. Until now the nearest read-only window was research-only, because task holds key on the task alone.

Suggested Design bullet: "The daemon advertises `tasks.sandbox.v1`; the CLI checks it before sending a sandbox target and refuses `task-hold-capability` (exit 6) against an older daemon."

### Mutants

Each mutant was applied alone to an export of 4cd213782 in scratch, never the worktree. The two later commits change only help strings.
- **Control:** the union of every mutant's tests passed on the pristine export, 234 tests. Every run imported the export (checked).
- **Restore:** after each run the target's bytes were restored, and SHA-256 matched the pristine file every time (`daemon.py` 20983326…, `cli.py` b09214b4…).

| Mutant | Killing test | Result |
|---|---|---|
| Mn: a release with no row wakes task-held jobs again | `test_a_release_with_no_row_of_its_target_changes_and_records_nothing[p5-sandbox-release]` and `[task-release]` | Killed: 2 failed, 4 passed. |
| Mc: capability check removed | `test_p6_a_new_cli_sends_no_sandbox_target_to_an_older_daemon` (all 24 cases), plus the updated `test_sandbox_hold_cli` call-order tests and the socket round trip | Killed: 30 failed, 19 passed. |
| Mp: push build queued before the hold read | `test_push_workspace_preparation_reads_sandbox_hold_in_real_store[sandbox-held-first-look]` and `[sandbox-held-re-offer]` | Killed: 2 failed, 2 passed. |
| m1: `_take_task_jobs` uses the new hold instead of `job_held` | `test_shorter_replacement_keeps_the_other_targets_later_end` (4 cases) and the two release controls | Killed: 6 failed, 61 passed. |
| m3: the release ignores the remaining hold | `test_releasing_one_composed_hold_keeps_the_pin_grace_paused` (4 cases) and `test_composed_holds_preserve_the_retry_deadline` (2) | Killed: 6 failed, 61 passed. |
| m2: drop `COALESCE(task,'')!=''` from the sandbox clause | none: equivalent | Survived: 234 passed. |
| m1+m2 together | `test_p4_the_sandbox_hold_commit_leaves_taskless_jobs_and_turns_queued` and the two release controls | Killed: 3 failed, 3 passed. |

**Why m2 is equivalent.** The clause feeds two readers:
- **The hold's commit (`_take_task_jobs`)** calls `job_held` for each row, and `job_held` returns None for a job with no task, so the mutant's extra rows are skipped.
- **The release** selects only jobs waiting `task-held`. No reachable state has a taskless job waiting `task-held`:
  - the only write of that wait is `_take_for_hold` (`daemon.py:1709`);
  - each caller gets its hold from `hold_for` or `job_held`, and both return None for a taskless job;
  - nothing updates `jobs.task` after submit.

The guard keeps the commit's read to task-bearing jobs and states the exemption. p4 kills the combination of m1 and m2, where both guards are gone.

### Commands and results

- **Setup:**
  - `git fetch origin release/217` (82d3dd819);
  - `git merge --no-commit --no-ff 82d3dd819…`: 3 conflicted files;
  - `env -u PYTHONPATH uv sync --group dev --python 3.12` with `UV_PROJECT_ENVIRONMENT` in scratch: CPython 3.12.14, pytest 9.1.1;
  - `uv pip install 'pytest-xdist>=3.6'` into that environment: 3.8.0.
- **Every pytest run** went through `fix-r1-run.py`, as `python -m pytest -q -p no:cacheprovider --basetemp <scratch>/pytest-<label>`. The environment was `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, `PYTHONPATH` unset, and Hypothesis storage in scratch. No run used more than 4 workers, and no two ran at once. Load is the 1-minute average from `uptime` at start and end. Every run finished: none was stopped by its alarm.

| Run | Files | Result | Time | Load |
|---|---|---|---|---|
| merge-1 | 8 merge-check files (listed above), `-n 4` | 592 passed, 9 failed | 174 s | 185 → 174 |
| merge-2 | `test_sandbox_hold_cli.py`, `test_sandbox_hold_contract.py`, `-n 2` | 46 passed | 97 s | 174 → 182 |
| rows-1 | `test_sandbox_hold_release_rows.py`, `-n 3` | 6 passed | 68 s | 109 → 124 |
| cap-1 | versions, sandbox CLI, sandbox contract, CAS CLI, CAS integration, `-n 4` | 149 passed | 86 s | 125 → 168 |
| push-1 | push sandbox hold, build, build budget, retry budget, store completion, versions, `-n 4` | 122 passed, 1 skipped, 1 failed (my assert's precedence bug) | 44 s | 183 → 174 |
| push-2 | `test_host_push_sandbox_hold.py` | 4 passed | 9 s | 173 → 172 |
| push-3 | store interleavings, storage recovery, push admission, `-n 4` | 41 passed, 2 failed (the pinned-order test) | 60 s | 174 → 178 |
| push-4 | that test after the hook change, `-n 2` | 2 passed | 19 s | 169 → 162 |
| push-all-1 | all 22 `tests/unit/test_host_push*.py`, `-n 4` | 402 passed, 1 skipped | 248 s | 156 → 177 |
| contract-1 | versions, `test_invariants_index.py`, `test_test_module_names.py`, `test_handler_refusals.py`, `-n 4` | 431 passed | 114 s | 167 → 193 |
| sel-a | 5 generated-property files of the selected set, `-n 4` | 86 passed, 6 skipped, 1 xfailed | 205 s | 188 → 186 |
| sel-b | 12 other `tests/fake` files of the set, `-n 4` | 444 passed | 367 s | 187 → 201 |
| sel-c | 9 `tests/unit` files of the set, `-n 4` | 263 passed | 88 s | 192 → 182 |
| bundle-a | `tests/fake/test_state_contract.py`, `-n 4` | 37 passed | 57 s | 176 → 193 |
| help-1 | sandbox CLI, CAS CLI, `-n 2` | 84 passed | 36 s | 188 → 185 |
| final-cli | sandbox CLI, CAS CLI, versions, `test_cli.py`, `-n 3`, at 1535cebbb | 371 passed | 101 s | 190 → 188 |

- **Selected set:** 26 files (`fix-r1-selected.txt`): every test file naming `task_holds`, `task-held`, `tasks hold`, `job_held` or `wait_hold`, plus the new release-rows, versions and CAS-integration files. Across sel-a, sel-b and sel-c: 793 passed, 6 skipped, 1 xfailed, 0 failed.
  - The 6 skips are the opt-in task-hold mutant matrix.
  - The xfail is the existing strict `test_c6_10_a_wait_of_another_kind_does_not_record_the_same_verdict_again`.
- **Contract bundle:** bundle-a plus contract-1's three files: 37 + 431 (431 includes the versions file).
- **Mutant runs** (`fix-r1-mutants.jsonl`, logs `fix-r1-log-mut-*`):
  - control: 234 passed, 83 s;
  - Mn: 2 failed, 4 passed;
  - Mc: 30 failed, 19 passed;
  - Mp: 2 failed, 2 passed;
  - m1: 6 failed, 61 passed;
  - m3: 6 failed, 61 passed;
  - m2: 234 passed;
  - m1+m2: 3 failed, 3 passed.

  Loads ran from 167 to 192.
- **Probes:** p6's daemon half against an export of 82d3dd819 (exit 0; 4 refusals, store unchanged) and p7 against the same export (exit 0). A first p7 attempt imported the worktree, not the export, and stopped on its own assert. The rerun set `PYTHONPATH` to the export.
- **Other checks:**
  - `python3 merge_contract.py` (above);
  - word-diff, clause-id and blank-line checks (Python, exit 0);
  - `subfleet tasks [hold|release] --help` rendered through the scratch interpreter;
  - `ast.parse` of `cli.py`;
  - SHA-256 check of the 33 evidence copies (0 mismatches);
  - `git status`, `log`, `diff` and `add` with named files only, and `git commit` (7 commits plus this report).

### Deviations

- **Start date:** the brief is dated 2026-11-14 (local), and the UTC start is 2026-11-15.
- **The merge commit** also carries the two test updates that #304's rules forced. It holds no fix for a finding.
- **Setup:**
  - I synced with `uv sync --group dev --python 3.12` and didn't try `--extra api`, which earlier rounds found absent.
  - xdist was installed into the scratch environment rather than with `uv run --with`.
- **Test grouping:** new tests ran in small groups, not one file at a time. The selected set ran as three `-n 4` runs, and the contract bundle as two, to stay inside the 600-second tool limit.
- **Before scratch existed,** a contract-check command wrote `/tmp/ac-85769b8….md` and `/tmp/ac-HEAD.md`. I removed both files at once with `rm -f`, not a guarded `rmtree`, because they were files. A third, `/tmp/ac-origin/release/217.md`, was never written.
- **Folders outside scratch:**
  - `subfleet/__pycache__`, `subfleet/adapters/__pycache__`, `subfleet/gate/__pycache__` and `subfleet/sessions/__pycache__` in the worktree were written by my `--help` checks. I moved them into scratch, and they were deleted with it.
  - `$(getconf DARWIN_USER_TEMP_DIR)uv-cache`, which the brief names, existed before this job (created 2026-11-14 09:13 EST), so I left it.
  - Test fixtures create and remove their own roots (`/tmp/sf-*`, `$TMPDIR/sf-th-*`).
- **Scratch:** deleted once the evidence was copied, with `shutil.rmtree` guarded on `sf-sbh-f1` (`exists: False` afterwards).
- **Not run:** the whole suite, Python 3.14 and CI. No process of mine remains, and nothing was pushed and no branch created or moved.
- **Commit rule:** every commit added named files only; the checkout stayed detached.

Code head: 1535cebbbdaef653da16044d414aa022cd665621. This report is the commit after it.
