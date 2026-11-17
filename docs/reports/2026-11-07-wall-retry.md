# D-WALL-RETRY1: outside faults return their wall time

An attempt that settles `limited`, `transient` or `auth-dead`, in a terminal state other than `quarantined` and with no stop, no longer spends its job's provider wall (`max_wall_s`). Every attempt keeps its own `max_wall_s` cap while it runs, and `max_attempts` bounds the retries. The build lane's work is committed on `fix/wall-retry-217` as 95b742856 (the rule, both readers and the contract), 3d4208c3b (properties, fake-provider tests and the mutant harness) and ae293db63 (this report and the lane's logs). The hub merged release/217 into the branch at 308b61984, 2d6db4246 and f5177bfde. Fix round 1 answers review r1 in the commits listed under "Fix r1", at the end. At its head the required regression list passed in full on CPython 3.14.7 and 3.15.2, a daemon-level test covers the job bound for a job whose every attempt ends `transient`, and all eleven mutants were killed in an exported copy of the source.

Built from release/217, `a8624a4899a2ce40c849cc5c6f7d74a7010f11b5`, in the build lane's isolated checkout. The build began 2026-11-11 at 17:02 UTC with a three-hour stop at 20:02 UTC. Its sandbox denied `index.lock`, so the lane could not commit, and the hub committed its files as the three commits above. Fix round 1 began 2026-11-15 at 01:42:42 UTC with a stop at 05:12:42 UTC. Neither round inspected or changed the live store, a live process, credentials or the caller's files.

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

An attempt that settles `lost` with class `transient` and no stop is exempt too, although the build brief listed `lost` among the counted outcomes. The hub ruled after review r1 to keep it exempt: no provider of the attempt is live once it settles, so it accrues no provider time, and while it ran it counted like any live attempt. D-WALL1 now says so.

`contracts.WALL_EXEMPT_OUTCOME_CLASSES` is the single class definition. Both `_running_s` and `LIVE_TICK` consume it and the same terminal-state set derived from `AttemptState`. The SQL uses `COALESCE(class IN (...),0)` inside its exclusion test, so NULL classes count. Its existing millisecond rounding allowance is unchanged.

Finalization calls `_running_s` with its finishing attempt's id, explicitly retaining that attempt's own interval for the overrun comparison. Earlier attempts follow the rule. No event or record key was added: cancel `running_s` remains the wall sum, and existing `wall_overrun` fields retain their meaning.

The build amended D-WALL1's first sentence, added its per-attempt cap sentence, added C-4.5's provider-time bound and wrote a change-log entry in the existing format. The entire sentence containing `last_observed_alive_at` was checked against HEAD and preserved byte for byte, leaving draft #299's edit separate. Fix round 1 added three things. D-WALL1 now says that the overrun record's `running_s` counts the finishing attempt's own time whatever its class. It carries the `lost`/`transient` sentence above. And C-4.5's bound sentence now follows the revive parenthetical, which again sits beside the sentence it qualifies.

## Invariants and evidence

The properties in `tests/unit/test_wall_retry_properties.py` each use Hypothesis with at least 200 examples and `deadline=None`, inserting generated rows into a real Store. Sequences include every AttemptState, every OutcomeClass and NULL, NULL/wall/client `killed_by`, absent/reversed timestamps, and at most one live attempt, last. Exact-sum properties include arbitrary microseconds; tick agreement uses canonical millisecond timestamps matching guardian receipts and retains the old SQL's floating-point tolerance.

| Invariant | Test |
| --- | --- |
| 1. Exclusion: the wall sum equals the reference sum over exactly the rows the rule counts. | `test_wall_retry_exclusion_matches_reference`; explicit examples include all exempt classes, stops, quarantine, lost/unknown. |
| 2. Per-attempt cap: each live provider's own interval stays in the sum, including an interim exempt class. | `test_wall_retry_live_attempt_keeps_its_own_time`. Through the daemon, `test_limited_retry_has_its_own_full_wall_cap`, `test_lane_fault_extra_attempt_has_its_own_wall_cap` and the "runs into the wall" case of `test_a_job_whose_every_attempt_ends_transient_is_bounded` stop at the attempt's own cap. |
| 3. Job bound: provider time is at most permitted attempts, including one possible lane fault, times `max_wall_s`, plus D-WALL1 overruns. | `test_wall_retry_provider_time_is_bounded_by_attempts_times_cap` asks production code for both halves of the bound. `_running_s`, searched over the clock, decides when the wall stops each live attempt. `_attempts_left` with `_uncharged` decides whether another attempt is admitted. Overruns are measured as finalization measures them, or as a late stop's cancel record would. Through the daemon, `test_a_job_whose_every_attempt_ends_transient_is_bounded` drives a job whose attempts all end `transient` (two cases), and the lane-fault test verifies one extra attempt with `max_attempts=1`. |
| 4. Monotone: with a fixed ledger the sum never falls as time advances; adding a terminal exempt row leaves it unchanged, and any other addition cannot lower it. | `test_wall_retry_fixed_ledger_and_additions_are_monotone`. Changing a live row to an exempt terminal row intentionally returns time and is outside the fixed-ledger premise. |
| 5. Agreement: the tick projection is at least the exact sum and at most 1 ms per counted prior above it, including NULL classes. | `test_wall_retry_tick_projection_agrees_with_exact_sum`; NULL, exempt, stopped, quarantined and lost/unknown priors have explicit examples. This projection uses a starting/running provider without an exit; finalizing age is not provider execution. |
| 6. Unchanged elsewhere: without a terminal exempt row, Python and SQL equal frozen copies of the old readers. | `test_wall_retry_nonexempt_ledgers_match_frozen_readers`, including arbitrary microsecond timestamps; old SQL is copied literally from the starting commit. |
| 7. Order-free: row order changes neither sum. | `test_wall_retry_row_order_does_not_change_either_sum`, using insertion permutations and SQLite `reverse_unordered_selects` on the writer connection. |

`test_wall_retry_reader_sees_class_written_in_same_transaction` verifies the finalization visibility premise. The fake-provider module also proves that ordinary lost time remains charged and that a transient finishing after its cap records its own full time while earlier outside faults are excluded. `test_finalization_time_is_not_charged_to_later_attempts` checks two priors. A `lost` prior counts, charged to its provider exit rather than its finalization. A `transient` prior gives its time back. One census test's `_running_s` stub accepts the new internal keyword.

## Incident replay

`test_wall_retry_incident_replay_gives_final_attempt_the_full_cap` replays the supplied incident durations through the independent reference. No live data was queried.

| Job | Excluded earlier provider time | Final attempt's full allowance | Used when stopped | Still available under this rule |
| --- | ---: | ---: | ---: | ---: |
| `20261104-210838-credit-integrate-r3` | 5,159 + 7,986 = 13,145 s | 21,600 s | 8,455 s | 13,145 s |
| `20261104-210909-search-rel-parser-build` | 5,312 + 8,080 = 13,392 s | 21,600 s | 8,209 s | 13,391 s |
| `20261106-053439-hub-rev-axiom-microsim-29` | 1,169.5 s | 21,600 s | 20,430.9 s | 1,169.1 s |

The one-second/fractional differences from the earlier-duration totals reflect the supplied independently measured stop durations. Each final attempt receives six hours of its own time.

## Build validation (summary)

The build lane's logs, mutant logs and command ledger were committed in ae293db63 and removed in fix round 1 (8b84fd835, 675 lines). Their results:

- **Properties:** 11 passed in 243.89 s. The strengthened SQL, differential and reversed-scan properties: 3 passed, 8 deselected in 309.50 s.
- **Fake-provider and wall tests:** 38 passed and 1 failed in 666.04 s. The failure was a fixture describing a natural exit after a wall stop. The receipt was corrected to carry signal 15 and `killed_by=max_wall_s`, and the lane-fault test then passed in 195.68 s.
- **Mutants 1 to 7:** each was killed by its named test (the table under "Fix r1" repeats them at this round's head). The build's harness edited the checkout's `subfleet/daemon.py` in place and restored it after each mutant, to SHA-256 `5b9b19c6…` at the build's head. Fix round 1 replaced that harness.
- **Final regression:** the 282-item list ran with `-n 4` on CPython 3.15.2. It was interrupted at 19:37 UTC (exit 130) after 159 completions, 157 passes and two failures. Its log held progress dots only, with no test names or tracebacks. Fix round 1 reran the list; see "The build's two failures".
- **Diagnostics:** a lane-fault diagnostic passed both selected cases (122.70 s). A smoke run of the module-names, invariants-index and census tests passed 27 of 27 (117.71 s).

## Build commands (summary)

- `uv sync --group dev` with the default cache failed: the sandbox denied `CACHEDIR.TAG`. A retry with a cache under the task's temp folder passed and installed CPython 3.15.2.
- `git switch -c fix/wall-retry-217` failed because the branch existed. `git switch fix/wall-retry-217` failed because the caller's worktree had it checked out.
- `git add` and `git commit` failed three times on a sandbox-denied `index.lock`. The hub committed the files.
- A scoped `pkill -INT` of the final batch returned exit 3 because `sysmond` was unavailable. The tool session's Ctrl-C then stopped the batch (exit 130).
- Read-only source and test inspection, `git diff --check` and `py_compile` checks passed.

## Remaining work and checkout state

- All the build's work is committed (95b742856, 3d4208c3b, ae293db63), as is fix round 1's (listed under "Fix r1"). Nothing is left uncommitted.
- The required regression list passed in full at this round's head: 285 of 285 on CPython 3.14.7, and again on 3.15.2. It also passed at the build's head, 282 of 282. The build's two failures could not be named and did not recur.
- The PR body's "Known gap" paragraph still says CI should name the two failures. It needs the hub's edit, which this lane cannot make. This lane does not push, so I did not check CI on the new head.
- The whole suite was not run locally; the brief leaves it to CI. It collects 27,285 tests with no collection error.

## Fix r1

Review r1 (`review-r1-lane.md`, head 308b61984) requested changes, with two P2 findings and five P3 findings. This round started from f5177bfde: review r1's head 2d6db4246 plus the hub's merge of release/217 (82d3dd819). It began at 2026-11-15 01:42:42 UTC. Production code (`subfleet/`) is unchanged in this round.

| Commit | Change |
| --- | --- |
| 793b72cef | Tests: the daemon-level all-transient bound, the production-driven invariant 3 property, the counted-prior finalization check |
| c8965870c | Contract: D-WALL1's overrun wording and the `lost`/`transient` sentence; C-4.5's sentence order |
| 2cdecf9b3 | Tools: the mutant harness runs on a `git archive` export |
| 13247f4d0, 85d3830a0 | Tools: the mutant harness prints each kill's one-line reason |
| 8b84fd835 | Report data: the build lane's logs removed |
| this section's commit | Report: corrected build sections, this section, `fix-r1-mutants.jsonl` |

### Findings and changes

**1. P2: the report described the lane's unfinished state, and the regression list had not passed at a head.**
- **The list.** It passed 285 of 285 at 2cdecf9b3 on CPython 3.14.7, in three parts. It passed 285 of 285 again at 85d3830a0 on CPython 3.15.2, the build's interpreter, in two parts. The list has 285 items now: the brief's 282 plus three cases this round added. See "Regression list by part".
- **The two failures.** No surviving record names them, and they did not recur; see "The build's two failures".
- **The report.** The first paragraph, "Remaining work and checkout state" and the last line now describe the committed state. The sentence "matching the daemon used for final regression" is gone. The build's results that cited removed logs are summarized under "Build validation (summary)" and "Build commands (summary)".

**2. P2: the invariant 3 property passed by construction.**
- **A test through the daemon.** `test_a_job_whose_every_attempt_ends_transient_is_bounded` (`tests/fake/test_wall_retry.py:201-261`) sets `max_wall_s` 10 and `max_attempts` 3.
  - Every attempt is finalized by `_finalize`, with a fake adapter that classifies `transient`, and `_admit` admits each retry.
  - Each attempt runs 9 s under `_process_attempt`'s wall check, which leaves it alone; the old sum would have stopped a2 and a3. `_running_s` reads only the attempt's own 9 s, and 0 once it settles.
  - Case "exits after a missed cap": a3 exits on its own at 13 s and records `wall_overrun` with `running_s` 13.0 and `overrun_s` 3.0. The job ends `failed`, rc 1.
  - Case "runs into the wall": the wall stops a3 at its own 10 s, with `running_s` 10.0, and the job ends `cancelled`.
  - In both cases, `_admit` 61 s later admits no fourth attempt. Provider time (31 s or 28 s) is at most 3 × 10 s plus the recorded overrun.
- **The property now calls production code.** `test_wall_retry_provider_time_is_bounded_by_attempts_times_cap` (`tests/unit/test_wall_retry_properties.py:289-389`):
  - `wall_stop_ms` searches the clock for the first millisecond at which `Daemon._running_s` reaches the cap. That is where the wall stops a live attempt, or later by a generated delay.
  - `Daemon._attempts_left` and `Daemon._uncharged` decide admission and whether an `auth-dead` ending is the job's one lane fault.
  - An overrun is `_running_s(finishing_attempt_id=…)` minus the cap, read after the terminal row is written, as `_finalize` computes it, or the late stop's sum.
  - The test itself states only C-4.5's list of classes that retry on an unpinned read-only job, and each ending's state and class. It calls `pytest.fail` if admission keeps retrying past nine attempts.
  - Explicit examples: the all-transient job above, a lane fault followed by a wall stop, and a counted `lost` prior followed by a late wall stop.
  - `load()` now writes `evidence_json` too, so lane-fault evidence reaches `_uncharged`.
- **Mutants.** Finding 2's two mutants (8 and 9 below) are each killed by both tests.

**3. P3s.**
- **The weakened test.** `test_finalization_time_is_not_charged_to_later_attempts` (`tests/fake/test_wall_running_time.py:219-250`) is parametrized.
  - A receipt with no return code settles a1 `lost`/`unknown`, which counts and which the read-only job retries. `_running_s` reads 3: a1's 2 s, ending at its provider exit at −2 s and not at its finalization at 0, plus a2's 1 s. `prior_running_s` reads 2.
  - The `transient` case keeps 1 and 0.
  - My first draft used rc 1 (`unknown`, job `failed`). Resetting that job to `running` broke the harness's C-15.1 notice check at teardown, so the counted case uses `lost`.
- **D-WALL1's body** (`docs/acceptance-contract.md:466`) now says: "that `running_s` counts the finishing attempt's own time whatever its class, and earlier attempts under the exclusion above".
- **C-4.5** (`docs/acceptance-contract.md:470`): the bound sentence now follows the revive parenthetical, which again sits after "…a read-only job's repeats only reading."
- **`lost` with class `transient` and no stop.** One sentence in D-WALL1 (`:466`) and one under "Rule and implementation" give the hub's reason. No code changed.
- **The mutant harness.** `tools/wall_retry_mutants.py` (192 lines) follows `tools/guard_search_rel_mutations.py`.
  - It exports HEAD with `git archive` under a `/sf-` scratch folder and mutates only the export.
  - Its pytest runs import the export, which it checks through `subfleet.__file__`.
  - It restores each edited file from the export's pristine bytes and compares them. It also compares the checkout's `daemon.py` and `contracts.py` hashes before and after, then removes the export.
  - It holds the build's seven mutants and review r1's four.
- **Lane logs.** The twelve files under `docs/reports/2026-11-07-wall-retry-data/` are removed (675 lines). They were the command ledger, `mutants.json`, seven mutant logs, the interrupted run's `final-tests.log`, `lane-diagnose.log` and `final-smoke.log`. The two summary sections above keep their results. The folder now holds only `fix-r1-mutants.jsonl`: the mutant harness's twelve output rows from this round, which the mutant table cites.

### The build's two failures

No record names them:
- The build's `final-tests.log` held progress dots only: `-n 4`, no `-rf`, interrupted before pytest's summary. With four workers, a dot's position does not map to a test.
- The salvage copy (`salvage-build/untracked.tar`) holds no `.pytest_cache`.
- The build's temp folder, which held that run's basetemp, no longer exists (`ls`: No such file or directory).
- The build's lane-fault diagnostic had already cleared its two suspects.

They did not recur:
- at this round's head, 285 of 285, on CPython 3.14.7 (load 65 to 116) and on CPython 3.15.2 (load 153 to 168);
- at the build's own head, ae293db63, exported with `git archive` and run on CPython 3.15.2 with `-n 4`: 282 of 282 (load 145 to 184).

So no run I could make attributes them to this PR, and without names there is nothing to run at the base. One unverified guess: the build's sandbox also denied `sysmond` (its `pkill` returned exit 3), so a test that lists processes could have failed there and nowhere else.

### Regression list by part

Every run used `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, `-n 4`, `-p no:cacheprovider -rfE`, a private `--basetemp` under `sf-wr-f1` named `pytest-<run>`, and an in-process 1,140 s alarm. Each ran under the hub's `suite_hold.sh`, which admitted it at once. All seven runs finished.

| Run | Head, interpreter | Files | Hold (UTC), load at start → end | Result |
| --- | --- | --- | --- | --- |
| part1 | 2cdecf9b3, 3.14.7 | `wall_running_time`, `wall_inspection_order`, `review_r223_wall_probes`, `wall_retry`, `wall_retry_properties`, the census test | 02:12:26–02:17:22, 65.6 → 115.5 | 95 passed in 288.38 s |
| part2 | 2cdecf9b3, 3.14.7 | `wall_cancel_races`, `wall_stop_deliveries`, `lane_fault`, `retry_for_good_refusals` | 02:17:34–02:18:45, 109.8 → 89.3 | 83 passed in 66.92 s |
| part3 | 2cdecf9b3, 3.14.7 | `workspace_transient`, `state_contract`, `daemon_verbs`, `invariants_index`, `test_module_names` | 02:18:53–02:19:42, 86.4 → 84.4 | 107 passed in 45.08 s |
| p315-1 | 85d3830a0, 3.15.2 | as part1 | 02:27:18–02:34:20, 157.2 → 153.0 | 95 passed in 413.95 s |
| p315-23 | 85d3830a0, 3.15.2 | as part2 and part3 | 02:34:31–02:36:17, 158.9 → 168.2 | 190 passed in 99.93 s |
| ae-1 | ae293db63 export, 3.15.2 | as part1 | 02:37:15–02:42:23, 183.5 → 165.3 | 92 passed in 301.63 s |
| ae-23 | ae293db63 export, 3.15.2 | as part2 and part3 | 02:42:47–02:47:19, 174.3 → 145.4 | 190 passed in 264.43 s |

The commits after 2cdecf9b3 change only `tools/` and `docs/reports/`, which no test in the list reads. `git diff --stat 2cdecf9b3 HEAD -- subfleet tests docs/acceptance-contract.md docs/invariants.json docs/invariants.md` prints nothing.

### Mutants

Each mutant was applied alone to a `git archive` export of the head, never to the worktree, and restored from the export's pristine bytes, with a byte comparison. The control, unmutated, passed all six named tests. Every restored `daemon.py` hashes to `3544655e…` and the restored `contracts.py` to `9596ca87…`, both equal to the checkout's. The checkout's hashes were unchanged after every run. The final run was at 8b84fd835, 02:49:33–02:54:04 UTC, load 111.2 → 99.4 (slot B); its rows are in [fix-r1-mutants.jsonl](2026-11-07-wall-retry-data/fix-r1-mutants.jsonl).

| # | Mutant (file) | Killed by | Failing check |
| --- | --- | --- | --- |
| 1 | `_running_s` has no exclusion; the SQL keeps it (`daemon.py`) | `test_wall_retry_exclusion_matches_reference` | property falsified |
| 2 | The SQL has no exclusion; `_running_s` keeps it (`daemon.py`) | `test_wall_retry_tick_projection_agrees_with_exact_sum` | property falsified |
| 3 | The SQL uses a plain `NOT IN`, so NULL classes drop out (`daemon.py`) | `test_wall_retry_tick_projection_agrees_with_exact_sum` | property falsified |
| 4 | A live attempt with an interim class leaves the sum (`daemon.py`) | `test_wall_retry_live_attempt_keeps_its_own_time` | property falsified |
| 5 | Stopped attempts leave the sum (`daemon.py`) | `test_wall_retry_exclusion_matches_reference` | property falsified |
| 6 | `lost` and `unknown` attempts leave the sum (`daemon.py`) | `test_wall_retry_exclusion_matches_reference` | property falsified |
| 7 | The finishing exempt attempt is left out of the overrun comparison (`daemon.py`) | `test_transient_overrun_counts_its_own_time_after_classification` | `test_wall_retry.py:152`, `KeyError: 'wall_overrun'` |
| 8 | Finding 2: a `transient` attempt's running time is never counted, including its own overrun comparison and a stopped transient (`daemon.py`, `_running_s` skips class `transient`) | `test_a_job_whose_every_attempt_ends_transient_is_bounded`, `test_wall_retry_provider_time_is_bounded_by_attempts_times_cap` | `test_wall_retry.py:253`: a3 records no `wall_overrun` |
| 9 | Finding 2: `_attempts_left` returns True, dropping the `max_attempts` check (`daemon.py`) | the same two | `test_wall_retry.py:234`: the job is `('waiting', None)`, not `('failed', 1)` |
| 10 | An exempt `transient` attempt is counted again: `transient` leaves `WALL_EXEMPT_OUTCOME_CLASSES` (`contracts.py`) | `test_a_job_whose_every_attempt_ends_transient_is_bounded`, `test_wall_retry_exclusion_matches_reference` | `test_wall_retry.py:226`: `_running_s` reads 9.0 after a1 settles, not 0 |
| 11 | The finishing attempt is left out of the overrun sum, whatever its class (`daemon.py`, `_running_s` skips `finishing_attempt_id`) | `test_a_job_whose_every_attempt_ends_transient_is_bounded`, `test_wall_retry_provider_time_is_bounded_by_attempts_times_cap`, `test_transient_overrun_counts_its_own_time_after_classification` | `test_wall_retry.py:253` and `:152` |

Two earlier runs gave the same kills. The first, at 2cdecf9b3, ran the control and mutants 8 to 11 (02:20:04–02:21:37, load 89.4 → 120.7). The second, at 13247f4d0, ran the control and all eleven (02:22:11–02:25:45, load 98.1 → 170.9); it printed its reasons with a path-prefix glitch, fixed in 85d3830a0.

### Commands and results

`S` is `$(getconf DARWIN_USER_TEMP_DIR)sf-wr-f1`. Test commands ran with `env -u PYTHONPATH`.

- `date -u; git status --short; git log --oneline -8; uptime` returned 01:42:42 UTC, a clean tree at f5177bfde, load 130.2.
- Read review r1, the build brief, review r1's brief, this report, `git diff 82d3dd819 HEAD`, the PR's test modules, `test_state_contract.reserve`/`receipt_fixture`, `tests/fake_adapter.py`, `_finalize`, `_attempts_left`, `_enforce_wall`, `utcnow`/`after`, `suite_hold.sh`, `uv_sync_hold.sh` and `tools/guard_search_rel_mutations.py`. All succeeded.
- `uv_sync_hold.sh env UV_PROJECT_ENVIRONMENT=$S/venv UV_CACHE_DIR=$TMPDIR/uv-cache uv sync --locked --group dev --python /opt/homebrew/bin/python3.14` passed in 14 s. `uv pip install --python $S/venv/bin/python 'pytest-xdist>=3.6'` installed xdist 3.8.0 and execnet 2.1.2. The venv is CPython 3.14.7 with the GIL.
- Focused run 1, an uncommitted draft: `$S/venv/bin/python -m pytest -q -p no:cacheprovider --basetemp $S/pytest-focus-01` with the invariant 3 property, the all-transient daemon test and the finalization test. Result: 5 passed and 1 error in 95.22 s, load 46.2 → 162.8. The error was the teardown notice check on my rc 1 draft.
- Focused run 2, the finalization test with `--basetemp $S/pytest-focus-02`: 2 passed in 3.65 s, load 150.4 → 142.8.
- A dry check applied each of the eleven mutant edits to the source text in memory: each matched once and compiled.
- `pytest --collect-only -q` of the list collected 285 tests in 18.90 s.
- The seven regression runs passed, as in the table above. Each was `suite_hold.sh /bin/zsh $S/run_part.sh <run> <files>`, run in pytest with `-n 4`.
- Three mutant harness runs, `suite_hold.sh env -u PYTHONPATH $S/venv/bin/python tools/wall_retry_mutants.py --scratch $S [--only …]`, each exited 0, as above.
- `uv_sync_hold.sh … uv sync --locked --group dev --python 3.15.2` built `$S/venv315`, plus xdist 3.8.0: CPython 3.15.2 with the GIL.
- `git archive --format=tar ae293db63 subfleet tests pyproject.toml docs bin | tar -x -C $S/export-ae293db63` produced 211 MB. `subfleet.__file__` resolved inside the export. A first export without `docs` and `bin` was removed with a guarded `shutil.rmtree`.
- `ls` of the build's temp folder returned No such file or directory. `tar -tf salvage-build/untracked.tar` listed no `.pytest_cache`.
- `git diff --check f5177bfde HEAD` passed.
- `pytest --collect-only -q` of the whole suite collected 27,285 tests with no error, in 367.60 s, load 144.0 → 182.0.
- `git rm` of the twelve data files passed. Each commit added named files only (`git add <files>`, `git commit -F -`); all succeeded.

### Deviations

- The brief didn't name the hub's `suite_hold.sh`; I ran every test run and every mutant run under it to share the host. Each was admitted at once.
- `pytest-xdist` is not in the dev group, so I installed it into both venvs with `uv pip install`, under `uv_sync_hold.sh`.
- The list has 285 items, not 282, because this round added three cases.
- To look for the two failures, I also ran the list on CPython 3.15.2 and at the build's head. The brief didn't ask for either run.
- The mutant harness runs pytest in one process, without `-n`.
- I did not rerun `test_handler_refusals` and `test_protocol_registry`, which review r1 ran; production code is unchanged in this round.
- I did not edit the PR body's "Known gap" paragraph or check CI. This lane neither pushes nor edits PRs.
- The report's last line cannot name its own commit, so it names that commit's parent.
- I created no folder outside scratch. pytest wrote git-ignored `__pycache__` folders inside this job's worktree; they stay. At the end, the scratch folder `sf-wr-f1` (venvs, export, basetemps, logs) was deleted with a guarded `shutil.rmtree` that asserts `sf-wr-f1` is in the path.

Head built: the commit that adds this section, whose parent is `8b84fd835`. It changes only this report and its data folder.
