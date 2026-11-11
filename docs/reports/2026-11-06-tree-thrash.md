# D-TREE-THRASH1: placement before a fresh tree

Work began in the preserved attempt at 2026-11-06 09:49 UTC, at base `a8624a4899a2` in the assigned detached worktree. This continuation resumed at 12:56 UTC and preserves that attempt's files. No live service state or external process was inspected or changed; tests and measurements use isolated roots and process stubs.

**Status:** implementation, contract amendments, five mutation kills and cost comparison are complete. Validation is incomplete at the time limit: the required suite encountered Git timeouts, and three targeted cases did not finish. No commits were possible because Git denied `index.lock`.

## Incident supplied in the brief

On 2026-11-06, 05:00–09:20 UTC, the external controller made 71 lane releases while 19 jobs waited; only four attempts were reserved. Of those releases, 59 were followed within 120 seconds by the disk brake, a median 16 seconds later, with median free-disk loss 2.5 GB (range 2.1–4.1 GB). The store held 571 `job.worktree_given_back` events with `why: "no lane"`, across 12 jobs. A two-second sampler saw waiting trees grow from zero to eight in eight seconds after one release, although its batch started only two attempts, then fall to zero within 17 seconds of the lane hold. These are supplied measurements, not independently reread live data.

The controller's one-lane, one-or-two-start batching stays unchanged.

## Design

A fresh detached writer with no tree uses its future tree path for the existing route and reservation checks. Its ordinary `_prepare_route` evaluation supplies the decision and basis. If FIFO, caps, probe approval, leases, retention fences and the ramp would permit reservation, an internal `_WorkspaceNeeded` exception rolls that transaction back. Git prepares the tree outside the store lock. The same decision and basis then enter the same checks again, with `_route_stands` accounting for intervening changes. Preparation consumes no route retry. No lease or attempt is recorded by the first check.

Every ordinary route evaluation reads a fresh store snapshot. Therefore a mid-pass lane hold stops subsequent cuts even when the pass's standing-refusal shortcut cached an open lane. The all-lanes-closed shortcut remains unchanged and incurs no added polling. The race read uses the cached desktop answer (`refresh=False`), so no registry refresh occurs inside the store transaction. If a lane closes during Git preparation and no lane can take the job, a fresh standing-refusal read marks its hold `unprepared`; C-6.20's sweep returns that tree only if a fresh eligibility check still finds no lane.

Existing trees, retries, in-place jobs, pushes and turns retain their previous preparation behavior. No new event field or refusal is introduced; `unprepared` and `refused_now` already exist.

Route reuse counters now count both placement checks for a fresh placement; the scheduler evaluation still occurs once.

C-6.20, C-6.8, C-6.10, C-6.11 and C-23.16 were amended with a 2026-11-06 change-log line. The stale first-look description was corrected in the daemon's sweep/refusal docstrings and the legacy sweep tests; the original build left the sweep implementation unchanged; Fix round 1 adds the eligibility recheck described below and retains its other safety checks.

Rejected: a separate speculative scheduler evaluation (duplicates work and can disagree); duplicating FIFO/lease logic into a preflight (two implementations could drift); holding the store lock over Git (blocks unrelated work); polling a full pin view for every job (adds closed-pass cost). Reusing the existing transaction's checks costs another set of checks for actual fresh placements; measurements below quantify that cost.

## Invariants

- **I1, cuts follow placements:** each job has at most one successful deferred preparation per pass. With valid preparations and otherwise stable non-lane constraints, cuts in a pass are bounded by reservations plus the sum of looks whose lane closes between the successful provisional check and reservation; multiple jobs may each contribute a race in the same pass. A hold before the placement check prevents a cut. The bound is tested under lane changes with valid preparations and otherwise static non-lane constraints; cancellation, workspace failures and unrelated lease/fence changes during Git are separate reservation races, not claimed to fit its lane-close term.
- **I2, order unchanged:** valid workspaces and the same available rows produce the same `(job, lane)` reservation order as the actual base admission method. Intended differences: workspace errors surface when placement first permits preparation; a lane hold arriving during a pass is honored before further cuts. A malformed workspace can therefore keep ordinary capacity/FIFO status until preparation becomes possible, where the base might already have failed it. Ordinary concurrent changes during Git can still change the final reservation.
- **I3, a started job has its tree:** preparation completes from submit's recorded HEAD and captures the existing baseline before reservation; the recheck never bypasses preparation.
- **I4, no Git for a provisional waiter:** a fresh job rejected by the provisional placement checks runs no workspace Git and cuts no tree. A change during preparation can leave a job waiting with a tree under the reservation recheck's hold and clock.
- **I5, holds unchanged:** the existing checks retain C-6.9's FIFO/lease ordering, C-6.10's capacity clock, C-6.11's labels, C-23.16's demand, C-6.17's disk door and C-8.4's retirement fences.
- **I6, a failing workspace still fails:** C-6.8's refusal, transient backoff and exhaustion handling still apply when placement permits preparation, potentially later than the first admission look. Deferring preparation neither spends a workspace retry nor clears its failure count.

## Tests

`tests/fake/test_admission_tree_thrash.py` adds examples and Hypothesis properties:

| Invariant | Example / property |
| --- | --- |
| I1 | Twenty two-start rounds with no lane slot cap (the controller alone limits starts); hold after the second reservation; one forced evaluation-to-reservation closure; generated single-lane sequences inject at most one labelled race per pass; the new two-pinned-lane example counts both racing looks. |
| I2 | Actual base method loaded from the frozen base fixture, compared on reservation order in the measured shape and 24 generated lane/model/pin/cap/output-lease scenarios. |
| I3 | One route evaluation per fresh placement; move caller HEAD after submit and verify actual tree HEAD, baseline tree and attempt evidence; generated HEAD movements. Provisional checks leave one committed reservation event and the final three leases. |
| I4 | Forbid Git subprocess calls and source-inspection helper calls for fresh no-slot, output-lease, disk-door and retention-fence waiters; verify no tree or attempt. Generated lane sequences also bound cuts. |
| I5 | Differential comparison of normalized holds, capacity clocks, wait records and reset-credit demand; existing FIFO, task hold, pin, timer, disk and retention tests in the required suite. |
| I6 | Permanent refusal and transient workspace timeout deferred until capacity appears; generated transient/permanent failures and retry limits verify terminal failure after the existing retry budget. |

The measured-shape test caches immutable source HEAD, top, common-directory and prefix answers, plus its clean baseline, while keeping actual `git worktree add` and the real safety sweep. The static fixture obtains those source answers from its known loose HEAD ref and simple `.git` directory; it guards that fixture shape and never caches allocated worktree paths. Synthetic generated scheduling scenarios use the same source answers. I4/I6 use synthetic occupied-slot setup; their waiting/error assertions still use the real admission path, and I4 explicitly forbids source-helper calls so the fixture cannot mask Git for a waiter. Separate I3 tests run complete real Git snapshots with moving HEADs. The measured-shape assertions require base 200 cuts / 40 reservations / 160 give-backs and head 40 / 40 / 0, with zero labelled closure races; these counts are not yet claimed as a completed measurement.

The continuation added `test_a_lane_closed_after_evaluation_is_rechecked_before_cutting_a_tree`: a closure after `_pick` returns but before the provisional reservation check prevents even the first cut. It passed separately (1 passed in 657.24 s; setup 207.42 s, call 371.49 s). An interrupted earlier new-module run completed two passes in 828.23 s: that closure test and `test_reopening_the_original_lane_during_preparation_reuses_its_original_evaluation` (JUnit case durations 507.211 and 506.073 s, running in parallel). Unnamed interrupted-worker entries are not counted. The two required unit modules passed together (26 passed in 54.93 s).

The first continuation's broad required-suite run was interrupted after 20 passes and 10 failures in 2,814.37 s, exit 130. All ten traces originate in Git commands exceeding the existing 60-second limits during submission or workspace setup; two are Hypothesis replay failures after such a timeout. An independent focused run had 3 passes and 4 failures in 1,344.21 s, exit 130; the four isolated daemon logs all record submission `git rev-parse` timeouts. These are not claimed as passing regressions or as product assertion failures. Durable logs are alongside this report.

The final required-suite attempt used file-based worker distribution and `-x --tb=short`: 49 passed, 3 failed in 725.64 s, exit 2. All three failed in submit on `git rev-parse --verify HEAD` exceeding 60 s: `test_c6_16_why_names_priority_class_and_policy[False]`, `test_c6_12_the_incident_a_job_pinned_by_an_ambiguous_email_does_not_stop_the_pass`, and `test_a_job_cancelled_before_any_attempt_gives_back_its_tree`. It stopped at those failures; the complete required suite has not passed. The final log and XML are `2026-11-06-tree-thrash-final-tests.{log,xml}`.

The new module's earlier serial run also failed initial submit inspection at the 60-second cap before its measured shape ran. Its final fixture alone now sets `workspace_git_timeout_s` and `worktree_add_timeout_s` to 300 s, to allow real Git under this host load. Production limits, existing-suite fixtures, injected workspace failures and retry budgets are unchanged. The final new-module run uses four workers so its generated properties can progress alongside the measured shape.

The final frozen targeted run stopped at 15:41:29 UTC. JUnit records **14 named passes, zero failures and zero errors**, over 3,485.722 s. Interrupt cleanup raised `ValueError: was already stopped`, making the command exit 1; this is an incomplete run, not an all-green exit.

| Completed final coverage | Seconds |
| --- | ---: |
| I1 generated lane changes, with labelled closure-race cuts bounded by one per pass | 543.443 |
| I2/I5 generated differential reservations, holds, capacity clocks and reset demand | 701.165 |
| I3 real submitted-HEAD, baseline, one-evaluation and reservation-evidence example | 572.143 |
| I6 generated transient/permanent errors and retry budgets | 300.366 |

The other ten passes cover closure after evaluation, real closure-during-preparation give-back, original-lane reopening, desktop registry lock safety, all four I4 no-Git waits, and both I6 examples. Three cases did not complete: `test_measured_shape_twenty_two_start_rounds_cut_only_the_forty_placed_trees`, `test_a_hold_after_the_second_reservation_stops_later_cuts_in_this_pass` (queued behind that long case), and `test_i3_generated_head_movements_keep_the_submitted_tree_and_baseline`.

An independent `tests/fake/test_admission_tree_thrash_head.py` runs the same real-Git twenty rounds for head alone and asserts the exact FIFO trace, 40 cuts / 40 reservations / zero give-backs. It was interrupted at the deadline, exit 130: zero completed tests, zero failures/errors, JUnit duration 2,481.714 s. It completed sixteen rounds. The final isolated-store snapshot was 42 submitted jobs / 32 reservations / 32 trees present / zero give-backs. The base comparison stopped during its sixth round: 20 submitted jobs / 12 reservations / 18 trees present / 40 give-backs. These are partial observations, not the required completed twenty-round measurements or final cut totals. Both full expected-count assertions remain unverified.

`docs/reports/2026-11-06-tree-thrash-review-handoff.json` retains the fourteen named passes, durations, three unfinished cases and final snapshots. All test sessions and benchmark/mutation workers were stopped before this report was finalized; no background work remains.

## Mutations

**Five of five executed mutants were killed**, after four healthy controls passed. Eight exact source mutants are defined; the five below ran only in an expendable Git archive with named source/test/runner overlays because commits were denied. The runner refuses a checkout, batches healthy controls, and requires both the selected failed regression and its intended assertion to count a kill. Import errors, Git/process timeouts and submission inspection failures are infrastructure errors, never kills.

All killing tests are in `tests/fake/test_admission_tree_thrash.py`:

| Mutant | Named killing test | Seconds |
| --- | --- | ---: |
| `eager-waiter-tree` | `test_a_fresh_waiter_runs_no_git_and_writes_no_tree[no-slot]` | 120.032 |
| `forget-racing-giveback` | `test_a_closure_race_with_a_desktop_lane_never_refreshes_the_registry_under_the_store_lock` (missing `unprepared`) | 157.622 |
| `swallow-workspace-refusal` | `test_a_failing_workspace_reaches_the_job_when_capacity_first_allows_placement[False]` | 145.194 |
| `refresh-registry-under-store-lock` | `test_a_closure_race_with_a_desktop_lane_never_refreshes_the_registry_under_the_store_lock` (registry refresh under writer lock) | 185.468 |
| `reuse-standing-decision-with-old-basis` | `test_reopening_the_original_lane_during_preparation_reuses_its_original_evaluation` (wrong `codex-2` reservation) | 135.262 |

Controls took 204.630 s; summed control/mutant subprocess time was 948.208 s. The final invocation exited 0 at 15:14:40 UTC. Each result has `intended_failure=true`, `infrastructure_error=false`, and a full failure log. The archived daemon was restored byte-for-byte to the checkout and the recorded daemon hash after the run. No mutant touched the checkout. The three I3 mutants remain defined but were not executed in this final campaign.

Repeated repository initialization was too slow on this host. The mutation archive alone therefore applied `docs/reports/2026-11-06-tree-thrash-fixture-template.patch`: each test copied a genuine committed, clean fixture repository into a new directory, with no worktree registrations. The runner records the template and fixture hashes. Its template HEAD was `3ea17ddcfa73eded86ffb4cfc204eb8a54e1b9f6`. Normal checkout fixtures were unchanged by that accommodation. These five mutation runs use synthetic workspaces: they test the marker, reservation order, refusal and no-cut behavior, and do not claim to run the real safety sweep or the I3 Git tests.

Earlier uncounted attempts included a 240 s control timeout, a direct healthy control passing in 231.23 s, interrupted slow controls, and a submit inspection timeout (pytest 629.92 s, runner 688.576 s). None counted as a kill. The JSON and six saved failure logs retain this evidence.

## Cost

The benchmark compares isolated 300-job passes in closed, controller-holds-after-two and fully-open states. Synthetic workspaces isolate daemon scheduling cost. Retired instructions come from `proc_pid_rusage` V4 `ri_instructions`; they count the worker process, excluding Git children. Wall and thread CPU time are also reported. Store query/one calls and returned rows are instrumented equally; direct transaction SQL is excluded from these partial read counts. Exact daemon SHA256: `a2d56e2efecb049a18780d8bb7cadb7f1877e181b48ecb7bf706955e17b52c07`.

**The change costs more when every fresh job is placeable:** retired instructions increased 27.49%, wall time 20.12%, and thread CPU time 25.63% in the fully-open pair. This is the extra reservation check after preparation, not a second route evaluation. The controller-shaped pair avoided 298 of 300 synthetic preparations with unchanged reservations and evaluation count; its instruction count fell 1.42%, while wall time increased 3.78% under load.

| 300-job scenario | Base retired instructions | Head retired instructions | Base / head wall seconds | Base / head thread CPU seconds |
| --- | ---: | ---: | ---: | ---: |
| Closed, median of two comparable pairs | 1,075,037,400.5 | 1,014,451,182.5 | 39.172278 / 33.892138 | 0.260582 / 0.258472 |
| Controller holds after two reservations | 9,102,963,208 | 8,973,961,854 | 105.3923 / 109.3773 | 1.355514 / 1.338993 |
| Fully open | 16,668,125,198 | 21,250,462,064 | 216.8099 / 260.4391 | 2.195656 / 2.758344 |

| Scenario | Store query/one reads, base / head | Returned rows, base / head | Evaluations, base / head | Preparations, base / head | Reservations, base / head |
| --- | ---: | ---: | ---: | ---: | ---: |
| Closed | 607 / 607 | 303 / 303 | 0 / 0 | 0 / 0 | 0 / 0 |
| Controller | 8,710 / 8,732 | 7,187 / 7,198 | 300 / 300 | 300 / 2 | 2 / 2 |
| Fully open | 9,007 / 12,307 | 228,154 / 274,504 | 300 / 300 | 300 / 300 | 300 / 300 |

The comparable pairs used the same frozen benchmark SHA256 `e87dfe9d767014e4091b84f72fdc10e104c8f657b53882564ccb443bf103187d` and the exact daemon hash above. **Closed passes showed no slowdown:** over two samples per version, median instructions fell 5.64%, wall time 13.48%, and thread CPU 0.81%. This small, noisy sample is not claimed as an algorithmic speedup. The second pair was chosen by a completion-time rule before its result was known. Bytecode inspection found identical pre-workspace opcode counts (2,600 instructions, 31 `EXTENDED_ARG` each), no new per-job checks on the closed shortcut, and three additional local slots in the pass's one function frame.

The first closed-lane pair had differing setup versions: base 1,060,411,888 / head 1,076,062,754 instructions (+1.48%), 42.2470 / 54.3892 s wall. That increase is recorded, but it is not a valid controlled comparison: setup-only seed-submission caching changed between workers and can change allocation/GC state. Every closed sample made 607 partial reads returning 303 rows and zero evaluations, preparations or reservations. Earlier partial results and setup provenance are retained in the cost JSON.

Setup-only optimizations cache known immutable Git answers during the seed submission and restore real helpers before measuring admission. The optional real-Git 300-job run in the preserved attempt was interrupted before a complete sample; no result from it is claimed.

## Commands and results

All commands run in the assigned worktree unless explicitly described as archive tests. Common environment:

```sh
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)subfleet-attempt-20261106-044747-tree-thrash-build-a2-8t9jwmf0/"
export UV_CACHE_DIR="$TMPDIR/uv-cache"
export PATH="/usr/bin:/bin:/opt/homebrew/bin"
```

- `uv sync --group dev`: passed, Python 3.14.7, nine packages installed; preparation 34.34 s, installation 1.57 s.
- Continuation `uv sync --group dev`: passed; resolved 10 packages in 4.31 s, checked 9 in 1.40 s. The preserved attempt used the matching `a1-bkcv4agq` session-temp child; the continuation uses the `a2-8t9jwmf0` child above.
- `uv run --no-sync python -m compileall -q subfleet/daemon.py`: passed.
- `/usr/bin/git --no-pager diff --check`: passed.
- Initial `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -n 4 -q tests/fake/test_unrun_worktrees.py tests/fake/test_admission_properties.py --basetemp "$TMPDIR/tree-thrash-first"`: interrupted as a redundant run after 41 passes in 1,996.39 s (exit 130).
- Three early test-helper probes each had two passes and two failures (174.82 s, 485.77 s, 455.57 s). The probes loaded helpers that reused a writable caller session or acquired an output lease before submission. Both helper errors were corrected before the final runs.
- A superseded new-file run was interrupted after nine passes in 1,736.84 s (exit 130). Its replacement uses the optimized measured shape and corrected test normalization.
- Desktop closure-race regression: one pass, 14 deselected, 75.42 s; validates the final off-lock desktop refresh behavior.
- Required suite: `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -n 4 -q tests/fake/test_unrun_worktrees.py tests/fake/test_admission_*.py tests/fake/test_task_hold_admission.py tests/fake/test_unadmittable_pin_admission.py tests/fake/test_timers_busy_admission.py tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py --basetemp "$TMPDIR/tree-thrash-required-resumed" --durations=15 --junitxml=docs/reports/2026-11-06-tree-thrash-tests.xml`: 20 passed / 10 failed, interrupted, as above. Final repeat adds `--dist loadfile -x --tb=short`, uses fresh `tree-thrash-required-last` and `2026-11-06-tree-thrash-final-tests.xml`: 49 passed / 3 submit-timeout failures.
- `uv run --no-sync pytest -q tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py --basetemp "$TMPDIR/tree-thrash-required-units" --durations=5`: 26 passed in 54.93 s.
- Repeated those two unit modules after adding the independent head-only test, using fresh `tree-thrash-final-units`: 26 passed in 42.09 s.
- Final targeted command: `PYTHONUNBUFFERED=1 uv run --no-sync --with 'pytest-xdist>=3.6' pytest -n 4 -q tests/fake/test_admission_tree_thrash.py --basetemp "$TMPDIR/tree-thrash-review-frozen" --durations=20 --tb=short --junitxml=docs/reports/2026-11-06-tree-thrash-review-frozen.xml`: 14 named passes, three unfinished cases, interrupted as above; output saved in the matching `.log`.
- Independent head command: `PYTHONUNBUFFERED=1 uv run --no-sync pytest -q tests/fake/test_admission_tree_thrash_head.py --basetemp "$TMPDIR/tree-thrash-review-head-only" --durations=5 --tb=short --junitxml=docs/reports/2026-11-06-tree-thrash-head-only.xml`, output saved in its matching `.log`; final partial result recorded above.
- Final compileall covered the daemon, both new test files, legacy sweep tests and both tools: passed. Final `git diff --check`: passed. A tests-only search found no existing exact comparisons for the new standalone mutation-artifact fields; product event/store fields and refusals were not added.
- `uv run --no-sync python tools/benchmark_tree_thrash.py --samples 1 --jobs 300 --head-overlay subfleet/daemon.py --output docs/reports/2026-11-06-tree-thrash-final-cost.json`: controller and open pairs above, plus the setup-mismatched closed pair.
- Two frozen-runner closed repeats add `--scenarios closed` and write `docs/reports/2026-11-06-tree-thrash-cost-overlay.json`: total command walls 16 min 26.86 s and 13 min 20.43 s including setup; measured pass medians are in the table. All eight headline samples and six summaries are merged in the final-cost JSON; all cost workers exited.
- From the expendable archive after the saved fixture patch, with `SUBFLEET_TREE_THRASH_REPO_TEMPLATE` pointing at its clean template: `WORKTREE/.venv/bin/python tools/tree_thrash_mutations.py baseline eager-waiter-tree forget-racing-giveback swallow-workspace-refusal refresh-registry-under-store-lock reuse-standing-decision-with-old-basis --timeout 600 --control-timeout 1200 --temp-root "$TMPDIR/tree-thrash-mutant-tests" --output WORKTREE/docs/reports/2026-11-06-tree-thrash-mutants-final.json`: four healthy controls, five intended kills, exit 0. `WORKTREE` denotes the assigned checkout; the runner's cwd was the archive, which had no `.git`.

No new refusal was added. The existing `unprepared`/`refused_now` hold comparisons were searched; their vocabulary and the sweep's safety checks are unchanged.

## Environment and deviations

`uv sync --group dev` installed the development dependencies. The prescribed shared Darwin `$TMPDIR/uv-cache` was denied by the filesystem sandbox. Runs instead set TMPDIR to the assigned writable session child of `getconf DARWIN_USER_TEMP_DIR`, and UV_CACHE_DIR to its `uv-cache`; archive tests use this path, without a literal `/tmp` component. Shells use `login=false`; Git operations use `/usr/bin/git` because the Homebrew Git executable was slow on this host.

Git did **not** permit committing. Named-file staging repeatedly failed with `fatal: Unable to create '/Users/maxghenis/subfleet-v2/.git/worktrees/20261106-044747-tree-thrash-build/index.lock': Operation not permitted`. Each conditional commit therefore did not run. Attempts covered the implementation/contract, tests, benchmark tool and cost artifacts; the final report and source remain in the assigned workspace for hub collection. No history was rewritten and nothing was pushed.

The deviations are incomplete real-Git twenty-round measurement and generated I3 completion at the time limit, the required suite's host-timeout failures, and the explicitly described fixture accommodations. No expected count is presented as an observed completed run. The 300-job synthetic controller benchmark and five mutation kills completed; actual Git preparation timing remains unmeasured.


## Fix round 1

Work began on 2026-11-11 at approximately 17:04 UTC from `a22ac891703147078f62329dfbd06da69650bb8b`, in the assigned detached workspace. Only supplied read-only evidence, repository objects and isolated test state were used. Git could not create a commit. Validation is incomplete. The original build results above are historical; this section records the fix round.

### Changes and CI failures

The three shard-1 failures depended on `git show a8624a4899a2:subfleet/daemon.py`. The tests now bundle the exact original `_admit_pass` in `tests/fixtures/tree_thrash_a8624a4899a2.py`, validate its SHA-256 at load time and compile it against the daemon namespace. The frozen method is byte-for-byte equal to the base: SHA-256 `a894326e79493fd6dc79b8fea75fd392add53d8283f123f4e49e328ee4afbcf5`; the original complete daemon hashes to `c78dfc024c802627dfd83f0504fa872baf6d11605b4d5dbab8d2e6818faa753e`. No test requires that historical Git object or an environment override. An archive with no `.git` imported its own daemon, and `git cat-file -e a8624a4899a2` exited 128 there (saved `history-proof.log`).

The shard-2 retention oracle encoded eager preparation when a retirement fence stood above the future allocated tree but not above the source or its dependencies: it expected `_workspace` to run once before waiting. The provisional transaction now detects that same fence using the predicted write target and waits before preparation. The relevant C-8.4 outcome remains no reservation under a fence; C-6.5 still leases only the allocated write target. The correction retains exact fence/hold attribution and strengthens checks of foreign holders, absence of attempts and job-held leases, and absence of an allocated tree. It does not permit any reservation previously forbidden by either clause. Reproduction and full-module results follow below.

For review r1's reopening finding, `_give_back_unrun` rechecks `scheduler.unadmittable(..., far_s=0)` under the detached pass lock before returning an `unprepared` tree. Fresh lane and closure rows replace the stale hold as the authority; desktop eligibility uses the same lookup as admission, with any refresh outside the store lock. Advisory cached hints alone cannot justify a destructive give-back. Terminal and disk-held trees retain their existing checks. The existing synthetic sweep property now supplies a real standing refusal when it models an `unprepared` hold. The permanent `test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree` cuts a real tree, closes during preparation, reopens without admission, and verifies that the sweep keeps it.

C-6.20, C-6.8, the change-log entry and this report now qualify I4: rejection by the provisional placement checks runs no workspace Git and cuts no tree. A lane, output lease, retention fence or capacity change during preparation can still leave a prepared job waiting under the reservation recheck's hold and clock. `test_the_contract_no_git_for_a_waiter_covers_a_lease_arriving_during_preparation` permanently demonstrates a real tree plus an output-lease wait. Workspace errors can remain undiscovered indefinitely while placement never permits preparation; their labels, clocks and workspace-ready notices can therefore occur later or be omitted when intervening capacity waits replace the workspace wait.

**Restated I1:** each job has at most one successful deferred preparation per pass. With valid preparations and otherwise stable non-lane constraints, cuts are bounded by reservations plus the sum of looks whose lane closes between the successful provisional check and reservation. Multiple jobs can each contribute a race in one pass. `test_i1_two_pinned_lanes_can_each_lose_a_prepared_tree_in_one_pass` demonstrates two preparations and zero reservations, one preparation per job. The original generated test deliberately injects at most one race per pass; that fixture bound is not a universal guarantee. Cancellation, workspace failures and unrelated lease/fence changes remain separate races.

The provisional placement path now skips `_slot_lease`: `_route_stands` still verifies that the pool has room, and final reservation selects the lowest available slot number. This removes the repeated one-lane slot search (45,150 extra SQL probes at 300 placements). The submitted-HEAD example spies on slot selection and requires exactly one call after the real tree exists. Pass pacing is unchanged. No product event/record key or refusal was added; existing `unprepared`/`refused_now` assertions were searched.

### Ordered checks

The initial archive and retention attempts used Python 3.15.2; the final retention retry, ordered suites and mutant campaign use CI's Python 3.14.7. Runs use uv's synced development dependencies, at most four pytest workers, a fresh `--basetemp` per run and `--timeout=1200`. The first archive run selected Homebrew Git; its measured-shape timeout prompted a retry with system Git. Subsequent runs set `PATH=/usr/bin:/bin:/opt/homebrew/bin`. The initial three-case run completed before retention reproduction began; its extra measured-shape retry overlaps only the next validation stage, with total pytest workers capped at four.

- `ordered-1`: 2 passed, 1 failed/errors, 0 skipped; XML wall 1519.722 s.
- `ordered-1-system-git`: 0 passed, 1 failed/errors, 0 skipped; XML wall 1390.183 s.
- `retention-reproduce`: interrupted; no confirmed completed cases. See retention-ready.json for commands and results.
- `retention`: interrupted; no confirmed completed cases. See retention-ready.json for commands and results.
- `retention-ci`: attempted; exit -9, interrupted for the run budget after 410.177 s; no confirmed completed cases. Interrupted JUnit placeholders are not passes.
- `ordered-3`: attempted; exit -9, interrupted for the run budget after 347.406 s; no confirmed completed cases. Interrupted JUnit placeholders are not passes.
- `ordered-4`: attempted; exit -9, interrupted for the run budget after 648.671 s; no confirmed completed cases. Interrupted JUnit placeholders are not passes.
- `ordered-5`: attempted; exit -9, interrupted for the run budget after 359.703 s; no confirmed completed cases. Interrupted JUnit placeholders are not passes.

Logs and JUnit files use the common prefix `docs/reports/2026-11-06-tree-thrash-fix-r1-`. The durable `checks.py` runner records exact commands, exit codes and budget interruptions in `checks-results.json`.

Latest recorded all-phase testcase times for the new modules (interrupted cases are not passes; unmeasured rows include their parameter variants):

| Test | Seconds | Result / run |
| --- | ---: | --- |
| `test_measured_shape_twenty_two_start_rounds_cut_only_the_forty_placed_trees` | 1211.547 | failed/incomplete; `ordered-1-system-git` |
| `test_a_hold_after_the_second_reservation_stops_later_cuts_in_this_pass` | — | No completed timing in this round |
| `test_a_lane_closed_after_evaluation_is_rechecked_before_cutting_a_tree` | — | No completed timing in this round |
| `test_the_one_cut_whose_lane_closes_between_evaluation_and_reservation_is_given_back` | — | No completed timing in this round |
| `test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree` | 345.255 | passed; `final-controls` |
| `test_the_contract_no_git_for_a_waiter_covers_a_lease_arriving_during_preparation` | — | No completed timing in this round |
| `test_i1_two_pinned_lanes_can_each_lose_a_prepared_tree_in_one_pass` | — | No completed timing in this round |
| `test_reopening_the_original_lane_during_preparation_reuses_its_original_evaluation` | — | No completed timing in this round |
| `test_a_closure_race_with_a_desktop_lane_never_refreshes_the_registry_under_the_store_lock` | — | No completed timing in this round |
| `test_a_fresh_waiter_runs_no_git_and_writes_no_tree` | — | No completed timing in this round |
| `test_the_placement_reuses_one_evaluation_and_pins_the_submitted_head` | — | No completed timing in this round |
| `test_a_failing_workspace_reaches_the_job_when_capacity_first_allows_placement` | — | No completed timing in this round |
| `test_i2_i5_generated_reservations_holds_and_capacity_clocks_equal_the_base` | 1008.206 | passed; `ordered-1` |
| `test_i1_generated_lane_changes_bound_cuts_by_reservations_and_the_labelled_race` | — | No completed timing in this round |
| `test_i3_generated_head_movements_keep_the_submitted_tree_and_baseline` | 233.180 | passed; `ordered-1` |
| `test_i6_generated_workspace_failures_keep_the_original_retry_limit` | — | No completed timing in this round |
| `test_head_twenty_two_start_rounds_cut_forty_trees_and_give_none_back` | — | No completed timing in this round |

### Cost and mutants

The open-pass instruction overhead fell from the previous 27.49% to 22.75%; it
still costs more than base. Thread-CPU overhead increased from 25.63% to 29.24%,
and the noisy wall-time difference changed from +20.12% to -47.86%. The new
open pair adds 3,726,453,150 instructions and 0.729959 CPU seconds, versus the
old pair's 4,582,336,866 instructions and 0.562689 CPU seconds. The interpreter
changed, so these paired percentages do not establish a clean raw timing
comparison between the old head and new head.

| New 300-job pair | Base / head instructions | Base / head wall seconds | Base / head thread-CPU seconds |
| --- | ---: | ---: | ---: |
| Closed, first pair | 927,913,886 / 1,073,641,565 | 36.801422 / 54.704116 | 0.220061 / 0.233947 |
| Controller holds after two | 8,881,967,315 / 9,070,657,161 | 145.442825 / 331.913457 | 1.561347 / 1.787616 |
| Fully open | 16,383,291,429 / 20,109,744,579 | 292.511738 / 152.517614 | 2.496475 / 3.226434 |

Counts match the earlier benchmark: closed makes 607/607 counted reads and
returns 303/303 rows; controller makes 8,710/8,732 reads and returns
7,187/7,198 rows, with 300/2 preparations and 2/2 reservations; open makes
9,007/12,307 reads and returns 228,154/274,504 rows, with 300 preparations and
reservations each. Direct transaction SQL remains outside these partial
counters. Removing the provisional `_slot_lease` search avoids the extra
45,150 slot-number probes in this 300-placement shape; the final reservation
still chooses its slot. The remaining 3,300 counted reads and 46,350 rows of
open-pass overhead remain visible.

The first closed pair was slower on head: base/head were 36.801422/54.704116
wall seconds, 0.220061/0.233947 thread-CPU seconds, and
927,913,886/1,073,641,565 instructions. Both made exactly 607 counted reads,
returned 303 rows, and performed zero evaluations, preparations or
reservations. This single noisy pair is retained, not replaced by a favorable
sample. The closed repeat also completed; its results and combined medians appear below.

`2026-11-06-tree-thrash-fix-r1-closed-prefix.json` proves exact base/head source
equality before the new preparation branch (SHA-256
`2f1725868dd305197fe64cc832e34dcac24c2729d5ad69f7dd33979fb5f389d0`). A
standalone AST compilation under Python 3.15.2 gives identical opcode-name
sequences, each 2,450 instructions with 26 `EXTENDED_ARG`; base/head have
163/166 local slots and 41 cells each. This is a static explanation of the
unchanged closed path, not a timing claim.

The two completed main-module cases in the initial ordered run took 1,008.206
and 233.180 seconds in its JUnit file. Their sum alone projects shard 1 to
55.586 minutes when added to the existing 34.897-minute map, already above the
40-minute warning threshold. The timed-out measured shape (1,222.396 seconds
all phases) is excluded from that lower bound. Final projection should use the
later completed full-module run when available. A split should extract the
long real-Git comparison/property cases into separate modules, then regenerate
and rebalance the shard map, adding a shard if the aggregate load requires it.

### Closed repeat (completed)

The scheduled closed-only repeat completed with exit 0. It started at
2026-11-11T18:40:50.855278+00:00 and used the same frozen runner and
method-identical daemon overlay `02f1ea4c...`. Base/head instructions were
1,034,563,042/1,061,104,894, wall seconds 37.915528/41.491435, and CPU seconds
0.332235/0.350248. Both again made 607 reads returning 303 rows, with no route
evaluation, preparation or reservation.

Across both retained pairs, base/head medians were
981,238,464.0/1,067,373,229.5 instructions,
37.358475/48.097776 wall seconds, and
0.276148/0.292098 CPU seconds. That is
**+8.78% instructions, +28.75% wall, +5.78% CPU**.
The current measurements show a closed-pass increase; they do not establish
the original no-slowdown goal. The identical prefix source/opcode evidence
and unchanged read/evaluation counts do not erase this measured result. No
pacing change was made. The two current pairs and their medians are retained
in `2026-11-06-tree-thrash-fix-r1-cost-comparison.json`.

### Mutation attempt and final-source controls

The authorized mutation campaign began at 19:29:00 UTC using isolated CI
Python 3.14.7, system Git, no xdist workers, and the archive without `.git`.
The four-test healthy baseline timed out after **691.376 seconds**,
pytest exit 124; `timed_out=true` and
`infrastructure_error=true`. The overall
runner exited 2. No completed testcase evidence or JUnit file was produced.
**Zero mutants were attempted and zero kills are claimed.** The archived daemon
was restored byte for byte. The baseline used the superseded sweep/profile and
output-key probe source, so it provides no final-source coverage.

The exact campaign command, from the archive, was:

```sh
"$TMPDIR/tree-thrash-r1/py314-env/bin/python" tools/tree_thrash_mutations.py \
  baseline reserve-without-tree lose-prepared-baseline evaluate-again-after-git \
  forget-sweep-eligibility-recheck --timeout 172 --control-timeout 688 \
  --control-test tests/fake/test_admission_tree_thrash.py::test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree \
  --control-test tests/fake/test_admission_tree_thrash.py::test_the_contract_no_git_for_a_waiter_covers_a_lease_arriving_during_preparation \
  --control-test tests/fake/test_admission_tree_thrash.py::test_i1_two_pinned_lanes_can_each_lose_a_prepared_tree_in_one_pass \
  --junit-dir "$WORKTREE/docs/reports" --temp-root "$TMPDIR/fix-r1-mutant-tests" \
  --output "$WORKTREE/docs/reports/2026-11-06-tree-thrash-fix-r1-mutants.json"
```

`WORKTREE` is the assigned workspace. The gate JSON stores the expanded exact
command. The earlier waiting drivers were interrupted before pytest started
when switching to CI Python and adding the extra control nodes; they are not
test results. Source compilation and `git diff --check` passed. A tests-only
search found no exact comparisons for this runner's new optional flags.

All intended killing tests below are in `tests/fake/test_admission_tree_thrash.py`.
These are test selections, **not executed kills**:

| Mutant | Intended killing test | This round |
| --- | --- | --- |
| `reserve-without-tree` | `test_the_placement_reuses_one_evaluation_and_pins_the_submitted_head` | Not run |
| `lose-prepared-baseline` | `test_the_placement_reuses_one_evaluation_and_pins_the_submitted_head` | Not run |
| `evaluate-again-after-git` | `test_the_placement_reuses_one_evaluation_and_pins_the_submitted_head` | Not run |
| `forget-sweep-eligibility-recheck` | `test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree` | Not run |

The final archive overlays were then refreshed to daemon SHA-256
`176f8c848fc0c36629d6c8518c65d4c436b5dc02358fbf7e904bc5eec1074774` and
test SHA-256 `5503f2cf143601a893e66f5441239a05d21921ab85e968710902d8fca2c1d269`.
The sweep mutant now removes the authoritative `_pin_view(self._desktop_identity())`
recheck; its anchor occurs exactly once. Exact source comparison still gives
`_admit_pass` SHA-256 `7589ddc6d76e7b8fd21e739f31c4924407d50ada3e4b99818679b4924712fe65`,
identical to the benchmark. The output-lease probe now uses the canonical APFS
identity key.

A final direct serial healthy run on that final source was launched with
`-vv -x -p no:cacheprovider`, the fresh basetemp `$TMPDIR/fix-r1-final-controls`,
and JUnit destination `2026-11-06-tree-thrash-fix-r1-final-controls.xml`. Its selected nodes, in
order, are the reopened-lane probe, lease-arrival probe, two-pinned-lanes probe,
and submitted-HEAD/I3 example. It was interrupted at 19:55 UTC, unfinished
and cleaned up by 19:57. Results and named times will be appended when available.

### Final control result and cleanup

The final serial run was interrupted on its own terminal at 19:55 UTC and
exited 1. Its verbose log confirms the reopened-lane probe passed and the
lease-arrival probe then started. The full collection did not pass.

| Final-source case | Seconds, all phases | Outcome |
| --- | ---: | --- |
| `test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree` | 345.255 | passed |

The lease-arrival, two-pinned-lanes and submitted-HEAD controls did not
complete in this final run. All four mutants remain not run; no kill is
claimed. All own benchmark, mutation and final-control commands have stopped.
The archived daemon still matches final checkout source byte for byte.

The existing CI map remains unchanged. The two earlier completed main-module
cases alone still give the documented 55.586-minute local projection for
shard 1; complete CI fit remains unproven. The other new-case times remain
unavailable when no completed testcase was recorded.

### Commands, environment and limits

```sh
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)subfleet-attempt-20261107-112016-tt-fix-r1-a1-tw4lucor"
export UV_CACHE_DIR="$TMPDIR/uv-cache"
export PATH=/usr/bin:/bin:/opt/homebrew/bin
```

- Read-only `cat` of the supplied report, build brief and full review r1: completed. Scoped `rg`/`sed` inspected admission, sweep, lease/fence helpers, tests, contract and tools; no `AGENTS.md` was present. GitNexus debugging skill was read, but no GitNexus tools were available, so source tracing was used.
- `git status --short`, `git branch --show-current`, `git rev-parse HEAD`: initial workspace clean and detached at the supplied head.
- `gh run view 42274833825 --repo MaxGhenis/subfleet-v2 --log-failed`: exit 0; saved `ci.log`, confirming both CI failure classes on both interpreters. `gh pr view 295 --repo MaxGhenis/subfleet-v2 --json body,title`: exit 0; saved original PR wording.
- `getconf DARWIN_USER_TEMP_DIR`: completed. Initial `uv sync --group dev` failed because the shared default cache denied `CACHEDIR.TAG`. `uv --cache-dir "$UV_CACHE_DIR" sync --group dev` in the permitted child succeeded, installing nine development packages. `uv run --no-sync --with 'pytest-xdist>=3.6' python -c 'import xdist; print(xdist.__version__)'`: passed, xdist 3.8.0.
- `/usr/bin/git archive HEAD | tar -xf - -C "$archive"`, followed by named daemon/test/frozen-fixture overlays and `test ! -e .git`: completed. The first three named cases ran there through `uv run --no-sync --project WORKSPACE --with 'pytest-xdist>=3.6' python -m pytest -n 3 -q ...`; the measured-shape retry uses the same archive serially and system Git. Both pass a new basetemp, durations, JUnit, short traceback and the 1,200-second cap. `history-proof.log` records the archive cwd/import and failing cat-file command.
- `python -m py_compile`/`compileall -q` of changed daemon, tests, frozen fixture and mutation tool: passed. Frozen-source AST extraction, byte comparison and SHA-256 checks: passed. Repeated `git diff --check`: passed. Existing hold comparisons were searched; no handler-refusal run is needed because no refusal was added.
- Named-file staging/commit attempts for daemon + sweep fixture, frozen oracle + permanent probes, and mutation tool all failed exit 128: the sandbox denies `/Users/maxghenis/subfleet-v2/.git/worktrees/20261107-112016-tt-fix-r1/index.lock`. No commit ran, no history was rewritten and nothing was pushed. Files remain for hub collection.



### Remaining validation and handoff

The required retention module, all cases of the main/head/unrun modules and the combined thirteen-module suite have not completed. Their final xdist attempts were interrupted during worker startup; no green suite is claimed. The twenty-round case timed out twice, so this round does not claim completed 200/40/160 versus 40/40/0 measurements. The two generated archive properties passed without the base object. New-probe timing claims appear only when a named case completes in the table above. The final serial controls are recorded in the tooling evidence. Four mutants remain not run, with zero claimed kills; prior review r1 kills are historical evidence, not executions in this round. Python 3.12 and a fresh Actions run were not run. The closed-pass measurements are slower on head and the no-slowdown requirement remains unproven; no pacing or CI-map change was made.

Final daemon SHA-256: `176f8c848fc0c36629d6c8518c65d4c436b5dc02358fbf7e904bc5eec1074774`. A final named staging attempt covering the daemon, four test files/fixture, mutation tool and contract was denied the same index lock. The final report and artifacts remain in the assigned workspace for hub collection.

Final ordered command ledger (the common environment above applies; each uses its own named log/XML):

- `uv --cache-dir '$TMPDIR/uv-cache' run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -n 4 -q tests/fake/test_detached_retention_fence.py --timeout=1200 --basetemp '$TMPDIR/tree-thrash-r1/retention-ci' --durations=0 --tb=short '--junitxml=$WORKSPACE/docs/reports/2026-11-06-tree-thrash-fix-r1-retention-ci.xml'`: exit -9; 410.177 s; budget interruption=True.
- `uv --cache-dir '$TMPDIR/uv-cache' run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -n 4 -q tests/fake/test_admission_tree_thrash.py::test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree tests/fake/test_admission_tree_thrash.py::test_the_contract_no_git_for_a_waiter_covers_a_lease_arriving_during_preparation tests/fake/test_admission_tree_thrash.py::test_i1_two_pinned_lanes_can_each_lose_a_prepared_tree_in_one_pass --timeout=1200 --basetemp '$TMPDIR/tree-thrash-r1/ordered-3' --durations=0 --tb=short '--junitxml=$WORKSPACE/docs/reports/2026-11-06-tree-thrash-fix-r1-ordered-3.xml'`: exit -9; 347.406 s; budget interruption=True.
- `uv --cache-dir '$TMPDIR/uv-cache' run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -n 4 -q tests/fake/test_admission_tree_thrash.py tests/fake/test_admission_tree_thrash_head.py tests/fake/test_unrun_worktrees.py --timeout=1200 --basetemp '$TMPDIR/tree-thrash-r1/ordered-4' --durations=0 --tb=short '--junitxml=$WORKSPACE/docs/reports/2026-11-06-tree-thrash-fix-r1-ordered-4.xml'`: exit -9; 648.671 s; budget interruption=True.
- `uv --cache-dir '$TMPDIR/uv-cache' run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -n 4 -q tests/fake/test_capacity_wait_backoff.py tests/fake/test_admission_latency.py tests/fake/test_admission_properties.py tests/fake/test_admission_stateful.py tests/fake/test_admission_visibility.py tests/fake/test_admission_liveness.py tests/fake/test_admission_route_isolation.py tests/fake/test_task_hold_admission.py tests/fake/test_workspace_transient.py tests/unit/test_daemon_verbs.py tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py --timeout=1200 --basetemp '$TMPDIR/tree-thrash-r1/ordered-5' --durations=0 --tb=short '--junitxml=$WORKSPACE/docs/reports/2026-11-06-tree-thrash-fix-r1-ordered-5.xml'`: exit -9; 359.703 s; budget interruption=True.

Additional commands: `UV_PROJECT_ENVIRONMENT="$TMPDIR/tree-thrash-r1/py314-env" uv --cache-dir "$UV_CACHE_DIR" sync --python 3.14 --group dev` passed (3.14.7, nine packages). Two idle check drivers were interrupted with exit 130 before starting any tests, to adjust the budget and interpreter; the final driver uses the saved command ledger above. `gh pr edit 295 --repo MaxGhenis/subfleet-v2 --body-file docs/reports/2026-11-06-tree-thrash-fix-r1-pr.md` passed and corrected the PR's race/no-Git wording. The retention attempt ledger in `retention-ready.json` includes its unsuccessful reproduction, cached-Python plugin error and interrupted startup; none is claimed as a pass.

Head built: `a22ac891703147078f62329dfbd06da69650bb8b` plus the preserved working-tree changes (commits denied).
