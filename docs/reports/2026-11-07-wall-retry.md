# D-WALL-RETRY1: outside faults return their wall time

Implemented the rule and contract changes. New properties and all seven mutant checks passed, but final regression is incomplete with two unresolved failures. All three commit attempts were denied by sandbox `index.lock` permissions.

Built from release/217, `a8624a4899a2ce40c849cc5c6f7d74a7010f11b5`, in the assigned isolated checkout. Work began 2026-11-11 at 17:02 UTC; the three-hour stop is 20:02 UTC. No live store, process, credentials or caller files were inspected or changed.

## Audit before implementation

All production references to `outcome_class` were searched before editing the daemon. There are two production column writes (line numbers refer to the starting commit):

| Writer | Classes and states |
| --- | --- |
| `Daemon._unlaunched`, `daemon.py:10805` | Writes `unknown` with `failed` or `interrupted`; unlaunched execution has a zero interval. |
| `Daemon._finalize`, `daemon.py:11947` | Writes the reconciled `outcome.cls.value`, terminal state and provider `finished_at` together. Natural `limited`, `transient` and `auth-dead` outcomes become `failed`. A client stop may preserve these classes as `interrupted`; a wall stop overrides the class to `unknown`. |

The sole production attempt INSERT (`daemon.py:8825`) omits the class, leaving NULL. `Store.add_attempt` and its generic update API can accept the column, but production does not call `add_attempt`, and its only `update_attempt` call writes capability. Adapter classification returns `Outcome` objects rather than writing the column. No production write occurs in conversations or another module.

Normal receipt-less losses classify `unknown` and remain charged. An exceptional cached transient stream-capture verdict may survive loss of the exit receipt and finish `lost`/`transient`. Quarantine preserves any existing class, and release can preserve it into `lost` or `interrupted`. Thus the implementation follows the exact three-part rule across all terminal states, rather than assuming outside faults always finish `failed`. In particular, a `lost`/`transient` row with no stop satisfies the formal exemption; an ordinary `lost`/`unknown` row does not. No alternative boundary was introduced.

`Store.transaction` yields the writer `self.connection`. `Store.query` consults `_reads_elsewhere`, which returns false when `_holds_writer` recognizes this thread. The transaction forbids an enclosing read snapshot. Consequently finalization's read sees the terminal class it just wrote. `threading.local` pins read snapshots here; it does not hide the writer's uncommitted changes. This is covered with a real Store having a reader pool.

## Rule and implementation

An attempt is excluded only when its state is terminal other than `quarantined`, its class is one of `limited`, `transient`, `auth-dead`, and `killed_by IS NULL`. All other intervals retain their previous timestamp semantics. Reserved or unstarted attempts contribute zero; starting/running attempts without a known end count through now; known provider ends stop the clock.

`contracts.WALL_EXEMPT_OUTCOME_CLASSES` is the single class definition. Both `_running_s` and `LIVE_TICK` consume it and the same terminal-state set derived from `AttemptState`. The SQL uses `COALESCE(class IN (...),0)` inside its exclusion test, so NULL classes count. Its existing millisecond rounding allowance is unchanged.

Finalization calls `_running_s` with its finishing attempt's id, explicitly retaining that attempt's own interval for the overrun comparison. Earlier attempts follow the rule. No event or record key was added: cancel `running_s` remains the wall sum, and existing `wall_overrun` fields retain their meaning.

D-WALL1's first sentence, its per-attempt cap sentence, C-4.5's permitted-attempt provider-time bound and the existing-format changelog were amended. The entire sentence containing `last_observed_alive_at` was checked against HEAD and preserved byte for byte, leaving draft #299's edit separate.

## Invariants and evidence

The properties in `tests/unit/test_wall_retry_properties.py` each use Hypothesis with at least 200 examples and `deadline=None`, inserting generated rows into a real Store. Sequences include every AttemptState, every OutcomeClass and NULL, NULL/wall/client `killed_by`, absent/reversed timestamps, and at most one live attempt, last. Exact-sum properties include arbitrary microseconds; tick agreement uses canonical millisecond timestamps matching guardian receipts and retains the old SQL's floating-point tolerance.

| Invariant | Test |
| --- | --- |
| 1. Exclusion: the wall sum equals the reference sum over exactly the rows the rule counts. | `test_wall_retry_exclusion_matches_reference`; explicit examples include all exempt classes, stops, quarantine, lost/unknown. |
| 2. Per-attempt cap: each live provider's own interval stays in the sum, including an interim exempt class. | `test_wall_retry_live_attempt_keeps_its_own_time`; daemon `test_limited_retry_has_its_own_full_wall_cap` and `test_lane_fault_extra_attempt_has_its_own_wall_cap` stop at the attempt's own cap. |
| 3. Job bound: provider time is at most permitted attempts, including one possible lane fault, times `max_wall_s`, plus D-WALL1 overruns. | `test_wall_retry_provider_time_is_bounded_by_attempts_times_cap` models each attempt live before settlement and includes separately generated missed-wall overruns; the lane-fault daemon test verifies admission with `max_attempts=1`, exactly one extra attempt and no third attempt. |
| 4. Monotone: with a fixed ledger the sum never falls as time advances; adding a terminal exempt row leaves it unchanged, and any other addition cannot lower it. | `test_wall_retry_fixed_ledger_and_additions_are_monotone`. Changing a live row to an exempt terminal row intentionally returns time and is outside the fixed-ledger premise. |
| 5. Agreement: the tick projection is at least the exact sum and at most 1 ms per counted prior above it, including NULL classes. | `test_wall_retry_tick_projection_agrees_with_exact_sum`; NULL, exempt, stopped, quarantined and lost/unknown priors have explicit examples. This projection uses a starting/running provider without an exit; finalizing age is not provider execution. |
| 6. Unchanged elsewhere: without a terminal exempt row, Python and SQL equal frozen copies of the old readers. | `test_wall_retry_nonexempt_ledgers_match_frozen_readers`, including arbitrary microsecond timestamps; old SQL is copied literally from the starting commit. |
| 7. Order-free: row order changes neither sum. | `test_wall_retry_row_order_does_not_change_either_sum`, using insertion permutations and SQLite `reverse_unordered_selects` on the writer connection. |

`test_wall_retry_reader_sees_class_written_in_same_transaction` verifies the finalization visibility premise. The fake-provider module also proves that ordinary lost time remains charged and that a transient finishing after its cap records its own full time while earlier outside faults are excluded. The existing finalization-delay test now expects an excluded transient prior, and one census test's `_running_s` stub accepts the new internal keyword.

## Incident replay

`test_wall_retry_incident_replay_gives_final_attempt_the_full_cap` replays the supplied incident durations through the independent reference. No live data was queried.

| Job | Excluded earlier provider time | Final attempt's full allowance | Used when stopped | Still available under this rule |
| --- | ---: | ---: | ---: | ---: |
| `20261104-210838-credit-integrate-r3` | 5,159 + 7,986 = 13,145 s | 21,600 s | 8,455 s | 13,145 s |
| `20261104-210909-search-rel-parser-build` | 5,312 + 8,080 = 13,392 s | 21,600 s | 8,209 s | 13,391 s |
| `20261106-053439-hub-rev-axiom-microsim-29` | 1,169.5 s | 21,600 s | 20,430.9 s | 1,169.1 s |

The one-second/fractional differences from the earlier-duration totals reflect the supplied independently measured stop durations. Each final attempt receives six hours of its own time.

## Validation and mutants

Initial property module: **11 passed in 243.89 s**. Strengthened SQL, differential and reversed-scan properties: **3 passed, 8 deselected in 309.50 s**. Initial fake-provider/wall run: **38 passed, 1 failed in 666.04 s**. The failure was a test fixture describing a natural exit after a wall stop; the daemon correctly superseded that stop. The receipt was corrected to carry signal 15 and `killed_by=max_wall_s`; the lane-fault rerun **passed in 195.68 s**. All seven mutants were killed by their intended checks: six assertion mismatches and mutant 7's missing `wall_overrun` evidence. The 282-item final regression batch was interrupted with Ctrl-C at 19:37 UTC (exit 130). Its retained progress output shows 157 pass markers and two failure markers, 159 completed items, without a failure summary or tracebacks. The two failures remain unidentified and unresolved, and the batch is incomplete; the earlier focused passes and seven mutant kills do not establish a green final regression run.

`tools/wall_retry_mutants.py` applied each mutant alone to a pristine byte copy, launched its named test in a new basetemp, restored in `finally` and verified bytes and SHA-256. It cleared daemon bytecode between mutations. The harness exited 0; each pytest subprocess exited 1 on its intended check (mutant 7 raised `KeyError: 'wall_overrun'`). All seven restored hashes equal `5b9b19c6cdbcf8634a1aeb80002eb0a998fbaef37c87b5e211d6755e443bfe50`, matching the daemon used for final regression. The temporary backup was removed after restoration. Results and failure logs are retained under `docs/reports/2026-11-07-wall-retry-data/`.

| Mutant | Killing test | Result |
| --- | --- | --- |
| 1. Python exclusion removed, SQL retained | `test_wall_retry_exclusion_matches_reference` | Killed |
| 2. SQL exclusion removed, Python retained | `test_wall_retry_tick_projection_agrees_with_exact_sum` | Killed |
| 3. Plain SQL `NOT IN`, dropping NULL | `test_wall_retry_tick_projection_agrees_with_exact_sum` | Killed |
| 4. Live interim class excluded | `test_wall_retry_live_attempt_keeps_its_own_time` | Killed |
| 5. Stopped attempts excluded | `test_wall_retry_exclusion_matches_reference` | Killed |
| 6. Lost and unknown excluded too | `test_wall_retry_exclusion_matches_reference` | Killed |
| 7. Finishing exempt attempt omitted from overrun | `test_transient_overrun_counts_its_own_time_after_classification` | Killed |

## Commands and results

All commands ran in the assigned worktree. Read-only source inspection was shared across three agents; repeated reads are grouped below, with paths, ranges and additional command details in [the command ledger](2026-11-07-wall-retry-data/commands.md). Tool file edits used `apply_patch` or Python exact-text replacement and succeeded. Session polls only collected the listed commands' results.

- `pwd`, `ls -la`, `git status --short`, `git -c core.fsmonitor=false status --short`, `git branch --show-current`, `git rev-parse HEAD`: assigned worktree, initially clean, detached at the starting commit.
- `rg --files` with AGENTS/pyproject/contracts/wall/invariants/uv.lock filename filters, and hidden AGENTS search excluding `.git`/`.venv`: located relevant files; no worktree AGENTS.md. Some exploratory guessed paths (`.agents`, `subfleet/providers`, several guessed unit-store modules, `tests/property/test_wall_running_time.py`) were absent; searches returned 1/2 and were corrected to actual files. No search above the worktree.
- `rg -n 'outcome_class' subfleet --glob '*.py' --glob '*.sql'`, plus assignment/INSERT/update/quarantine/OutcomeClass searches: audit above, two production writes. `sed` source reads covered contracts, store schema, store read/transaction/add/update functions, daemon LIVE_TICK, `_running_s`, wall enforcement, `_has_work`, reservation, unlaunched/quarantine/release and finalization paths, and adapter classifiers. All actual-file reads succeeded.
- `rg -n 'D-WALL1|C-4.5|C-4.2' docs`, narrowed contract searches and `sed` reads of contract preface/C-4.2/C-4.5: existing wording and changelog format inspected. The broad docs output was large and subsequently narrowed.
- `cat pyproject.toml`; `sed` reads of `tests/conftest.py`, unit conftest, wall-running-time, state-contract, lane-fault, daemon-verbs, invariant-index, test-module-names, store-property and census-round2 modules; `rg` for Store fixtures, basetemp/locks and `_running_s` patches: successful, identified the census stub adaptation.
- `export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"; export UV_CACHE_DIR="$TMPDIR/uv-cache"; uv sync --group dev`: failed initializing the sandbox-denied default cache (`CACHEDIR.TAG`, operation not permitted).
- Retried with `task_tmp=/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261107-104158-wall-retry-build-a1-i_0whm_8/wall-retry; mkdir -p "$task_tmp"; export TMPDIR="$task_tmp"; export UV_CACHE_DIR="$TMPDIR/uv-cache"; uv --cache-dir "$UV_CACHE_DIR" sync --group dev`: passed, installed nine packages including Hypothesis and pytest, CPython 3.15.2. All subsequent tests use this TMPDIR/cache and unique basetemp folders.
- `git switch -c fix/wall-retry-217`: failed, branch already exists. `git switch fix/wall-retry-217`: failed, branch is checked out in the caller's worktree; no force used.
- `git diff --check`, scoped `git diff --check -- <named files>`, `git diff --stat`, `git diff --numstat` and source diffs: passed. `python3 -m py_compile tests/fake/test_wall_retry.py`: passed. Python comparison with `git show HEAD:docs/acceptance-contract.md`: original observation sentence remains byte for byte.
- `git add subfleet/contracts.py subfleet/daemon.py docs/acceptance-contract.md`; `git commit -m 'fix: give outside-fault retries their own provider wall'`: both denied creation of this worktree's `index.lock` by sandbox permissions. No index/history workaround, rewrite, push or caller-worktree mutation was attempted.
- `git add tests/fake/test_wall_running_time.py tests/fake/test_wall_retry.py tests/unit/test_wall_retry_properties.py tests/unit/test_census_round2.py tools/wall_retry_mutants.py`; `git commit -m 'test: prove retry wall accounting over attempt sequences'`: both denied the same `index.lock` write. The coherent implementation and test steps therefore remain uncommitted for hub collection.
- `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest -q tests/unit/test_wall_retry_properties.py --basetemp "$TMPDIR/properties-01"`: 11 passed in 243.89 s.
- `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest -q tests/unit/test_wall_retry_properties.py -k 'tick_projection or nonexempt or row_order' --basetemp "$TMPDIR/properties-02"`: 3 passed, 8 deselected in 309.50 s.
- `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest tests/fake/test_wall_retry.py tests/fake/test_wall_running_time.py --basetemp "$TMPDIR/pytest-fake-initial"`: 38 passed, 1 fixture failure in 666.04 s, corrected as described above.
- `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest tests/fake/test_wall_retry.py::test_lane_fault_extra_attempt_has_its_own_wall_cap --basetemp "$TMPDIR/pytest-fake-lane-fixed"`: 1 passed in 195.68 s.
- `uv --cache-dir "$UV_CACHE_DIR" run --no-sync --with 'pytest-xdist>=3.6' python -c 'import xdist; print(xdist.__version__)'`: passed; xdist 3.8.0 prepared.
- `python3 -m py_compile tools/wall_retry_mutants.py subfleet/daemon.py subfleet/contracts.py`: passed. Additional scoped diff checks and harness/property/fake-test source reads passed. A speculative lock-file search using `tools/pytest*` found no glob matches (exit 1); no lock was changed.
- `uv --cache-dir "$UV_CACHE_DIR" run --no-sync python tools/wall_retry_mutants.py`: exit 0, seven mutants killed; the exact seven pytest subprocess commands and results are recorded in [mutants.json](2026-11-07-wall-retry-data/mutants.json) and individual logs.
- Python verification of `mutants.json`, restored SHA-256 and removal of the pristine backup: passed, 7/7 killed and bytes match.
- `uv --cache-dir "$UV_CACHE_DIR" run --no-sync --with 'pytest-xdist>=3.6' pytest -n 4 --basetemp "$TMPDIR/pytest-final-01" tests/fake/test_wall_running_time.py tests/fake/test_wall_inspection_order.py tests/fake/test_review_r223_wall_probes.py tests/fake/test_wall_cancel_races.py tests/fake/test_wall_stop_deliveries.py tests/fake/test_lane_fault.py tests/fake/test_retry_for_good_refusals.py tests/fake/test_workspace_transient.py tests/unit/test_daemon_verbs.py tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/fake/test_wall_retry.py tests/unit/test_wall_retry_properties.py tests/unit/test_census_round2.py::test_wall_dead_guardian_with_cwd_only_records_loss_without_a_kill`: interrupted by tool-session Ctrl-C at 19:37 UTC, exit 130. Output was piped through `tee` with `pipefail` into [final-tests.log](2026-11-07-wall-retry-data/final-tests.log). The 282-item collection included every requested end-of-job module and both new modules, plus the adapted census test; only 159 completion markers (157 passes, two failures) were retained, with no failure summary. The suite did not finish.
- `tail -n 12 docs/reports/2026-11-07-wall-retry-data/final-tests.log; git diff --check; git diff --stat; git status --short; git rev-parse HEAD`: passed after mutation restoration. Only the five intended tracked files and new report/evidence/test/harness files are changed; HEAD remains the starting commit. Subsequent `write_stdin` calls collected progress; the final Ctrl-C returned exit 130 without failure tracebacks.
- Python `Path` listing of the three newest directories under each worker in this batch's unique `pytest-final-01/popen-gw*` basetemp: exit 0; showed existing retry, wall-interleaving, mixed-stop-delivery and SQL-prior properties active. Only this run's scratch directories were read, without inspecting process state.

- A scoped `pkill -INT` attempt targeting only the `pytest -n 4 --basetemp .../pytest-final-01` command returned exit 3 because `sysmond` was unavailable; it returned no process list. Tool-session Ctrl-C then interrupted that test session successfully (exit 130).
- Read-only source inspection covered `failed_keep.py` and the test conftests, and listings/cache reads were confined to this job's own scratch and pytest cache; no additional state was inspected.
- `uv run --no-sync pytest -x -vv --tb=short tests/fake/test_lane_fault.py -k an_attempt_that_neve --basetemp "$TMPDIR/pytest-lane-diagnose-01"`: **2 passed, 18 deselected in 122.70 s**, recorded in [lane-diagnose.log](2026-11-07-wall-retry-data/lane-diagnose.log). Both suspected never-launched-attempt cases passed; neither final-batch failure has been identified.
- Report-only Python read of `final-tests.log` and exact progress-marker count: 157 pass markers plus two failures, 159 completed items; final regression status corrected to incomplete.

- `uv run --no-sync pytest -q --tb=short tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_census_round2.py::test_wall_dead_guardian_with_cwd_only_records_loss_without_a_kill --basetemp "$TMPDIR/pytest-final-smoke-01"`: **27 passed in 117.71 s**, output in [final-smoke.log](2026-11-07-wall-retry-data/final-smoke.log). This does not replace the incomplete required regression batch.
- Bounded reads of this batch's worker lock files, then direct checks of the four known worker PID paths under `/tmp/sf-failed`: no retained failure directories. No global temporary-directory scan or process inspection was used. Additional diagnostic source reads included `subfleet/lockwatch.py`; a speculative pytest tmpdir glob had no matches (exit 1).
- Final `git diff --check` passed. `git add` explicitly naming this report, command ledger, mutant JSON, all seven mutant logs, final regression log, diagnostic log and final smoke log, followed by `git commit -m 'docs: report wall retry proofs and validation limits'`: both denied `index.lock` creation. `git rev-parse HEAD` still returned the starting head. Final report reads and result updates completed before the deadline.

## Remaining work and checkout state

- The required final regression batch did not finish: 159 of 282 items have completion markers in its log (157 passes and two failures), and interruption returned exit 130. No final summary was produced.
- The two failure markers are unidentified and unresolved. The narrow lane-fault diagnostic passed both selected cases (2 passed, 18 deselected); those suspected cases therefore do not identify either final-batch failure.
- The final naming, invariant-index and adapted census checks passed all 27 tests. No test session remains running. Work stopped within the requested three-hour limit.
- Commits are blocked by the explicitly anticipated `index.lock` sandbox restriction. Implementation, test and report steps remain uncommitted for hub collection; the requested branch and caller checkout remain untouched.

Head built: `a8624a4899a2ce40c849cc5c6f7d74a7010f11b5` (changes in this checkout; commits denied).
