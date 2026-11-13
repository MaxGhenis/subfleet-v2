D-ADMIT-IDLE1 — 2026-11-07

Implemented both admission changes in the assigned workspace, preserving the detached checkout and existing files. Git refused to create the worktree's `index.lock` outside the writable workspace, so no commits were created. Separate `change-1.patch` and `change-2.patch` in the adjacent `2026-11-07-idle-admission-data` directory preserve the two review steps and were verified against a clean archive; report and measurement tooling form the final delivery step. No live state or live process was inspected or changed.

Change 1 rests after a successful detached pass reserves nothing. For pass duration D, the rest R is `max(2 s, 4D)`, measured from its end. Reservations run again on the next tick; exceptions retain only C-5.10 pacing. The independent turn worker keeps its cadence. Connection-local SQLite TEMP triggers recognize committed queue arrivals, lease deletions, closure changes and lane changes, including raw SQL from workers and timers. Nested rollback publishes no wake. Pilot answers and lapses explicitly wake admission; policy hashes are compared in memory. The next closure expiry is cached once per pass, including expiries during the first or a slow pass. Malformed deadlines remain job-specific routing errors and cannot hide a valid expiry. Due-job clocks never cancel the rest.

Change 2 compares active closure conditions, lane state/enrollment and policy hash once per admission pool per pass. A disappearing closure or changed lane/policy brings every eligible capacity wait forward, preserving probe-own and other C-6.10 exceptions. Committed closure/lane revisions also retain changes occurring between censuses, and failed passes retain capacity news until a whole pass succeeds. This revision guard uses Change 1's store notification infrastructure. Only an entirely event-only rejection verdict gets the 600 s ceiling; everything else keeps 30 s. Both waiting paths retain earlier known resets. The unprepared path evaluates its full route only when due, without preparing a tree or writing a decision, because standing refusals alone omit dynamic rejection reasons.

The operator audit found daemon handlers for `lanes hold`, `release`, `release --limits`, `enroll` and `transfer`. Their CLI commands send daemon operations. This release has no `lanes enable`, `disable`, `remove`, or runtime policy-reload verb. `policy.json` is loaded only at startup; `lanes.json` is seeded at startup and does not update existing enabled flags at runtime. No new operator commands were added. Semantic lane writes through the daemon's store and in-memory policy hash replacement wake immediately; direct writes through another connection are discovered by pass reads. Filesystem edits do not gain a reload mechanism. Audit details and original source locations are in `2026-11-07-idle-admission-data/audit.md`.

The corrected C-6.10 bound for reading-only capacity is **30 s + R + one control tick**, excluding the pass's own evaluation/queue traversal time. There is no fixed bound independent of D. Observed event changes cancel R and bring eligible waits forward on that pass. A missed notification costs at most one rest before the next census. “Within one tick” assumes the detached worker is available; an existing pass still finishes first.

`_reset_demand` has no freshness test on `checked_at`, `since` or `rechecks`: it qualifies `lane_wait` and the latest hold, then routes against current store data. Its answer is unchanged between looks. `why` truthfully exposes the older latest-look timestamp and longer next clock; it applies no freshness refusal. `daemon.status` and C-6.11's idle line use separately refreshed aggregate holds and current capacity. The not-due path still walks the job, checks task/pin/door conditions, derives retry demand, rebuilds FIFO/probe/lease ordering, refreshes `behind` and standing refusals, and supplies the hold to C-6.20's give-back sweep. It skips workspace preparation, full route evaluation and wait-clock writes.

An unchanged tree-bearing wait's `attempt.reserved` transaction updates `jobs.state='waiting'`, `wait_reason` and `next_check_at`. It suppresses the duplicate decision, creates no attempt and takes no reservation lease. The changed job row still makes `Store.transaction` append an `attempt.reserved` audit event with `{}`. The event name therefore does not prove a reservation occurred.

Every scheduler rejection is classified conservatively:

| Reason | Ceiling | Basis |
| --- | ---: | --- |
| `disabled` | 600 s | Observed lane state change. |
| `needs a shell: MODEL runs SANDBOX jobs without one` | 600 s | Lane/policy capability change. |
| `closed:SCOPE:UNTIL`, every matching cause `operator-hold` | 600 s | Observed release/expiry; known reset still clips the clock. |
| Same closure rendering, `provider-limit` | 30 s | Provider reading/time. |
| Same closure rendering, `credits` | 30 s | Reading/time. |
| Same closure rendering, `cooldown` | 30 s | Time. |
| Same closure rendering, `auth-dead` | 30 s | Conservative; recovery notification not established. |
| Unknown or unmatched closure cause | 30 s | Conservative default. |
| `excluded` | 30 s | Conservative. |
| `desktop` | 30 s | Desktop/login reading. |
| `config-dir` | 30 s | Conservative. |
| `owner-v1` | 30 s | Conservative. |
| `identity-mismatch` | 30 s | Conservative. |
| `credential-refused` | 30 s | Conservative. |
| `no-slot` | 30 s | Slots, fleet/parent/provider caps, probe/pilot guards. |
| `below-floor` | 30 s | Headroom reading. |
| `reserve:MODEL:unmeasured` | 30 s | Reading. |
| `reserve:MODEL:reserved` | 30 s | Reading/reset. |
| `reserve:MODEL:probe-required` | 30 s | Probe evidence. |
| Derived `unknown`, `no-lanes`, `credential-latched`, `auth-dead` | 30 s | Conservative. |
| Summary/hold labels `provider-cap`, `fleet-full`, `parent-cap`, `slot-kept`, `provider-slot-kept` | 30 s | Capacity/lease changes. |
| Any future, missing or non-string reason | 30 s | Total default. |

Every lane and every reason must qualify. Empty evaluations, candidates, missing evidence and mixed verdicts keep 30 s. A provider-limit sharing an operator hold's scope and deadline cannot inherit 600 s.

Synthetic measurements use 100 waiting jobs, four operator-held lanes and at least 1,000,000 event rows. Half the jobs exercise unprepared writable waits; half exercise real read-only workspace/reservation transactions. A real Daemon control loop and scheduler run in the measured process. Provider probes, conversation ticks and retention are suppressed to isolate admission. CPU is measured inside that process with `time.process_time` and `getrusage`; child Git CPU is excluded. Jobs are staggered across the actual steady-state ceiling after warm-up. All 100 remain waiting, with zero attempts.

| Metric | Before, a8624a489 | Final after |
| --- | ---: | ---: |
| Real wall seconds | 120.069263 | 120.088752 |
| Detached passes | 949 | 56 |
| Detached passes/minute | 474.226279 | 27.979306 |
| Process CPU seconds | 10.138547 | 1.275699 |
| Process CPU seconds/minute | 5.066349 | 0.637378 |
| Events committed | 401 | 20 |
| Events/hour | 12,023.060352 | 599.556568 |
| `os.lstat` calls/pass | 3.683878 | 14.500000 |
| Total `os.lstat` calls | 3,496 | 812 |

On this fixture, CPU rate fell 87.4%, passes 94.1% and event rate 95.0%. The final after run used a frozen copy whose production hashes matched this workspace at both start and end. Its 20 events were ten of each waiting-transaction kind.

The original before run committed 200 `attempt.reserved` and 201 `job.unprepared_wait` events. These synthetic timings are measurements on a shared development host, not estimates for the live host's 81% baseline. Slower admission cadence can increase lstat per pass because the two-second session-registry cache expires between passes; total calls are also reported. Raw JSON, source fingerprints, profiles and the reproducible harness are retained in the data directory and `tools/benchmark_idle_admission.py`.

One baseline pass with all 100 jobs due took 6.121267 s under cProfile/caller instrumentation. Its top 15 cumulative functions were:

| Function | Calls | Cumulative seconds |
| --- | ---: | ---: |
| `Daemon._admit` | 1 | 6.121699 |
| `Daemon._admit_kind` | 2 | 6.121693 |
| Harness `counted_pass` | 1 | 6.121277 |
| `Daemon._admit_pass` | 1 | 6.121011 |
| `Daemon._workspace` | 50 | 4.256525 |
| `salvage._git` | 100 | 4.255065 |
| `subprocess.run` | 100 | 4.137713 |
| `subprocess.Popen.__init__` | 100 | 2.276223 |
| `subprocess._execute_child` | 100 | 2.232101 |
| `salvage.git_head` | 50 | 2.198696 |
| `salvage.git_tree` | 50 | 2.056791 |
| `subprocess.communicate` | 100 | 1.845544 |
| `subprocess._communicate` | 100 | 1.830426 |
| `subprocess._communicate_io_posix` | 100 | 1.696073 |
| `selectors.select` | 200 | 1.678262 |

Wrapping `os.lstat` counted 736 calls: 500 through `posixpath.realpath` from `folders._spelled` (250 via `_detached_folders`/`folders.present`, 250 via admission's `folders.canonical`); 200 from `folders._kernel_path`; 36 from `home_paths` validating the synthetic session-registry path. This attributes the fixture's calls, not every call in the live native sample.

The final all-due profile took 7.741647 s with 748 lstat calls; its all-not-due profile took 64.972 ms with zero calls. These profiling timings are also noisy. The baseline all-not-due 100-job pass took 8.428 ms and made zero lstat calls. It still called `_retry_pin` and `list_attempts` once per job. At N=25/100/200, due lstat counts were 168/736/1,424 and not-due attempt-history reads were 25/100/200. Corresponding due times were 0.592/6.121/3.241 s and not-due times 0.00745/0.00843/0.03281 s; shared-host noise prevents inferring a timing slope. Counts establish that per-pass cost grows with waiting jobs. Resting reduces how often that cost is paid.

Fake-clock wake measurements:

| Source | Delay to next detached start |
| --- | ---: |
| Submit | 0.05 s |
| Retry enters queue | 0.05 s |
| Task hold released | 0.05 s |
| Lease released | 0.05 s |
| Closure added | 0.05 s |
| Closure released | 0.05 s |
| Closure reaches `until_at` | 0.05 s |
| Lane enabled | 0.05 s |
| Lane disabled | 0.05 s |
| Lane enrolled | 0.05 s |
| Lane removed | 0.05 s |
| Policy hash changed | 0.05 s |
| Pilot answer | 0.05 s |
| Reading-only change, short idle pass | 2.001 s from previous pass end; rest is not cancelled |

The reading test also makes a job due during the rest and verifies that this does not wake admission. No fresh-reading wake was added.

Tests for all eight invariants follow. `pacing` means `tests/fake/test_idle_admission_pacing.py`; `capacity` means `tests/fake/test_idle_capacity_events.py`.

| Invariant | Tests/evidence |
| --- | --- |
| 1, rest from end for max(2,4D) | pacing `test_idle_pass_rests_from_its_end_for_at_least_four_durations`, D=.01/.8/5 s. |
| 2, wakes within one tick | pacing `test_each_semantic_wake_starts_detached_on_the_next_tick`, all 13 sources; wake-during-pass, first/slow-pass expiry and malformed-expiry regressions. |
| 3, reservations and turns keep cadence | pacing `test_reservation_keeps_next_tick_and_turns_keep_their_cadence`; `test_failed_pass_has_only_worker_failure_pacing`. |
| 4, bring every eligible wait forward | capacity `test_c6_10_capacity_events_bring_every_eligible_wait_forward`, three waits plus probe exception, seven changes; `test_c6_10_a_condition_seen_only_after_the_census_still_brings_waits_forward` and `test_c6_10_a_failed_pass_keeps_capacity_news_for_every_wait_on_retry`. |
| 5, total classification | capacity `test_c6_10_every_rejection_reason_has_a_conservative_classification`, `test_c6_10_only_an_operator_closure_is_event_only`, mixed-verdict and duplicate-cause tests. |
| 6, steady-state commit bound | capacity `test_c6_10_long_wait_commits_at_most_six_times_per_hour_per_job`: four jobs, exactly 24 commits over 3,600 fake seconds after reaching the ceiling. |
| 7, old/new differential | capacity `test_c6_10_released_hold_places_the_old_rules_jobs_in_order_no_later`: release at t=17; new starts t=17, old t=30, identical four-job order. |
| 8, demand differential | capacity `test_c23_16_demand_is_identical_between_old_and_long_capacity_looks`, ages 0/29/31/299/599 s. |

Invariant 6 requires a qualification. Retaining the specified 1,2,4,... doubling produces 15 looks in the first hour of a fresh unchanged verdict, including t=0, rather than six plus one. The same ramp restarts when a verdict changes. The implementation preserves doubling and proves six/hour once at 600 s; an unconditional first-hour bound would require changing that requested ramp. C-6.10 and its changelog explicitly document steady state.

Mutation tests run only temporary source copies. A passing baseline and actual JUnit assertion failures are required; setup/collection errors do not count as kills.

| Mutant | Killing test |
| --- | --- |
| Offer every tick, no rest | `test_idle_pass_rests_from_its_end_for_at_least_four_durations[0.01]` |
| No submit wake | `test_each_semantic_wake_starts_detached_on_the_next_tick[submit]` |
| No released-closure wake | `test_each_semantic_wake_starts_detached_on_the_next_tick[closure-release]` |
| 600 s for every verdict | `test_c6_10_reading_or_time_verdicts_keep_the_30_second_ceiling[provider-limit]` |
| No released-closure bring-forward | `test_c6_10_capacity_events_bring_every_eligible_wait_forward[released]`; mutant removes census and revision fallback. |
| Bring forward every pass | `test_c6_10_no_change_never_brings_waits_forward` |
| Unknown reason event-only | `test_c6_10_every_rejection_reason_has_a_conservative_classification[future-rejection-False]` |

Final mutation baseline: **79 passed in 24.654 s; 7/7 mutants killed**, each selected mutant test exiting 1 with an assertion failure and no fixture/collection error. The mutation runner exited zero. Recorded production and new-test hashes match the final workspace; no mutant remains in it.

Commands and results:

- `uv sync --group dev` succeeded with the permitted cache; nine packages installed. Default Darwin temp/cache writes were denied. Tests subsequently used unique `/private/tmp/idle-admission-build` children; the final run also overrides TMPDIR to a permitted child because ProbeCancel explicitly allocates directly beneath it.
- First pacing/store/route-isolation check: 129 passed. Store-observer check: 16 passed; store IDs/properties/readers regression: 57 passed. Expiry/malformed-closure regression check: 24 passed. Focused existing expectation updates: seven passed. Capacity checks progressed from 53 to 55 and then 56 cases as races were added.
- First required-file run plus new tests/refusals: 976 passed, six failed, five skipped, one xfailed. Three failures were old lane-enable waiting expectations, one a 30 s-only stateful oracle, one the denied temp directory, and one a real malformed-expiry bug. All were addressed before the final rerun.
- Final required-file run plus the three new files and refusal checks: **986 passed, 5 skipped, 1 xfailed in 318.10 s**, exit zero (`required-final-2.xml`/`.log`). The first patch alone, applied to a clean baseline copy, also passed all 39 pacing/observer tests in 17.11 s. Both patches passed `git apply --check` and applied sequentially to a fresh archive; every affected final file matched this workspace byte for byte.
- `git diff --check` and Python parsing/compilation checks passed. No new event/record key or refusal was introduced; `tests/unit/test_handler_refusals.py` was nevertheless included.
- `git archive a8624a4899a2` supplied the isolated baseline. Benchmark commands selected that archive or this workspace with `--jobs 100 --events 1000000 --seconds 120 --profile`; 25/200-job profile comparisons used `--seconds 0`. Timed runs exited zero. Exact argv, counters and profiles are retained.
- `uv ... run --no-sync python tools/idle_admission_mutations.py` ran the isolated mutation checks; exact argv, source hashes, counts and killing assertions are in `2026-11-07-idle-admission-data/mutations.json` and logs.
- `git add subfleet/daemon.py subfleet/store.py tests/fake/test_idle_admission_pacing.py tests/unit/test_admission_store_wakes.py docs/acceptance-contract.md && git commit -m 'Pace idle detached admission and wake on committed changes'` failed before staging: `Unable to create .../.git/worktrees/20261107-071703-idle-admission-build/index.lock: Operation not permitted`.
- The final report-only `git add docs/reports/2026-11-07-idle-admission.md && git commit -m 'Report idle admission profiling and validation'` attempt failed at the same `index.lock`, again before staging.

The final required-file command, using at most four workers, was:

```sh
TMPDIR=/private/tmp/idle-admission-build/test-tmp-final \
UV_CACHE_DIR=/private/tmp/idle-admission-build/uv-cache \
uv --cache-dir /private/tmp/idle-admission-build/uv-cache run --no-sync --with 'pytest-xdist>=3.6' pytest -n 4 \
  tests/fake/test_capacity_wait_backoff.py tests/fake/test_admission_latency.py \
  tests/fake/test_admission_properties.py tests/fake/test_admission_stateful.py \
  tests/fake/test_admission_visibility.py tests/fake/test_admission_liveness.py \
  tests/fake/test_admission_route_isolation.py tests/fake/test_task_hold_admission.py \
  tests/fake/test_reset_credit_incident.py tests/fake/test_unadmittable_pin_review.py \
  tests/fake/test_probe_cancel.py tests/fake/test_unrun_worktrees.py \
  tests/unit/test_daemon_verbs.py tests/fake/test_state_contract.py \
  tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py \
  tests/fake/test_idle_admission_pacing.py tests/fake/test_idle_capacity_events.py \
  tests/unit/test_admission_store_wakes.py tests/unit/test_handler_refusals.py \
  --basetemp /private/tmp/idle-admission-build/required-final-2 \
  --junitxml docs/reports/2026-11-07-idle-admission-data/required-final-2.xml -q
```

Overlap with #295 is limited to the due unprepared wait and the ordinary wait-metadata call later in `_admit_pass`; no unrun-sweep lines changed. Neither `_batches` nor `_record_owned` changed, avoiding the daemon-quiet build's named areas.

Deviations: commits were blocked by filesystem permissions; separate verified patches preserve the review split. Runtime enable/disable/remove/reload commands do not exist in this baseline and were not invented. The six/hour bound is steady state, with the exponential-ramp exception explained above. Baseline profiling used frozen committed source while initial implementation proceeded in parallel, so its completed profile followed the first pacing edits. All measurements and tests used synthetic state. The whole suite was not run.


## Fix r1

Started 2026-11-13 12:15:43 UTC (`date -u`, first command); deadline 15:15:43 UTC. Work remained in the assigned detached workspace at `9e753f8fb7ef`. All daemon instances targeted synthetic state roots; a scratch-probe fixture omission is recorded below. Nothing was pushed.

Finding 1: `subfleet/daemon.py:1173,7753,7891,8166` retains one coalesced capacity-news flag per eligible pending waiter before any guard can skip it. The pool can acknowledge its census while a skipped waiter retains its flag. The capacity-clock check consumes it; `subfleet/daemon.py:8987` restores it if the reservation-time ramp holds without replacing the old clock. Loss of eligibility and pending-job cleanup prune it; restart forgets it and existing recovery makes ordinary waits due. Storage is at most one flag per waiter encountered in a pending snapshot, independently of the number of events; ended or ineligible waits are pruned by the next cleanup or visit. Probe-own waits and non-capacity clocks retain their exclusions. The new guard tests use real machine, disk and ramp decisions with synthetic readings and include both 30-second controls, conversation/reaper skips and coalescing/cleanup cases. A post-clock audit also found the real reservation-time ramp guard: after a backward clock step during workspace preparation, it can hold while leaving the old 600-second clock unchanged. The scratch reproduction failed once (29.00 s); the permanent `test_release_news_survives_ramp_recheck_after_clock_moves_back` now passes, and the full guard module passed 16 tests (25.92 s).

A Python AST walk enumerated all 12 outer job loop `continue` statements before the capacity-clock check; helper-source reads checked their clock transitions: workspace reaping; task holds and restoration races; route-error clocks; conversation holds; stuck pins and restoration races; permanently unadmittable retries; machine/disk/ramp holds; saturated pools/FIFO predecessors; approval/uncertain waits. Task, pin, route and approval cases establish their own clocks or restore a due job, rather than preserve an ordinary eligible capacity wait. Post-clock lease/folder/workspace guards establish their own clocks. The attempt-live check protects lifecycle invariants: reservation makes the job running, finalization sets its retry clock, and quarantine makes it terminal, all atomically. The late ramp was the additional ordinary capacity-wait exception and now preserves news. `tests/fake/test_idle_capacity_queue_news.py` covers `test_release_news_survives_pool_saturation` and `test_release_news_survives_older_waiter`. `docs/acceptance-contract.md:501` now promises the first pass that permits the capacity check, retaining news behind guards; it does not promise admission while a guard still holds.

Finding 2: `subfleet/daemon.py:8228` clears `_route_deferrals` after a successful unprepared route, matching the prepared path. Both requested tests in `tests/fake/test_idle_route_deferrals.py` have prepared controls. Before the fix: 2 failed, 2 passed (173.07 s); after: 4 passed (159.28 s). Isolated errors now restart at 5 seconds and the released closure admits at that retry time.

Finding 3: `docs/acceptance-contract.md:9,10,501` retains the existing global capacity-news refresh behavior and qualifies both C-6.10 and the original change-log entry with “without capacity-news refreshes.” Filtering the lane census alone would leave the global committed revision unchanged; safely scoping that revision requires broader observer and route/shared-cap decisions. Existing tests explicitly require unrelated enrollment/removal to refresh eligible waits. `tests/fake/test_idle_capacity_refresh_bound.py` preserves the stated exception for both paths: seven unrelated Claude lane changes cause seven Codex wait commits in 70 seconds, with unchanged verdict and 600-second backoff. Its first run passed 2 tests (170.37 s). A new contiguous change-log entry records the fixes.

Commands and results (the final file-by-file command ledger is `2026-11-07-idle-admission-data/fix-r1-validation.json`):

- First command: `date -u; pwd; git status --short`, followed by `cat` of the named review, brief and report: start 12:15:43 UTC, assigned workspace, initially clean. Documentation reads succeeded. No root `AGENTS.md` exists; the focused `rg --files` discovery initially included a literal `*` path and exited 2, then succeeded without it. Focused `rg`, `sed`, `cat`, `head`, AST inspection and source-hash reads examined only the named reviews, workspace and scratch.
- `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR="$TMPDIR/uv-cache" uv sync --group dev` failed to initialize the denied default cache (exit 2). Fallback `TMPDIR=/private/tmp/sf-ia-f1/test-tmp UV_CACHE_DIR=/private/tmp/sf-ia-f1/test-tmp/uv-cache uv --cache-dir /private/tmp/sf-ia-f1/test-tmp/uv-cache sync --group dev` succeeded: 9 packages installed. `uv run --no-sync --with 'pytest-xdist>=3.6'` supplied 7 additional packages for four-worker runs.
- After exporting those fallback variables, focused tests used `uv --cache-dir "$UV_CACHE_DIR" run --no-sync pytest FILE --basetemp /private/tmp/sf-ia-f1/NAME --junitxml /private/tmp/sf-ia-f1/NAME.xml -q`; worker runs additionally used `--with 'pytest-xdist>=3.6'` and `-n 4`. Every run used a new base temp. Route `route-reset-before`: 2 failed/2 passed; `route-reset-after`: 4 passed. Initial guard `guard-before`: 2 intended assertion failures, 3 ramp fixture-signature failures and 4 passing controls (238.63 s); the helper was corrected, and `guard-after`: 15 passed (122.87 s). Pool `.venv/bin/python -m pytest -q tests/fake/test_idle_capacity_pools.py --basetemp=/private/tmp/sf-ia-f1/pools-baseline-1`: 3 passed (94.95 s); bound `refresh-bound-01` (without `--junitxml`): 2 passed (170.37 s); queue `queue-news-after`: 2 passed (81.80 s). No test bound changed.
- The initial 25-file, one-file-at-a-time validation completed with **696 passed, 5 skipped, 1 xfailed**, zero failures/errors; exact commands and per-file counts are in `fix-r1-validation-initial.json`. That run began before the two-line late-ramp restoration, so the final-source repeat is recorded separately.
- The repeated final-source run passed its first five files, then its liveness differential progressed only 16 examples in roughly half an hour. To preserve the hard deadline, the tool session was interrupted with Ctrl-C (runner exit 130); 31 completed-case dots and no failure summary were retained in `fix-r1-liveness-repeat-interrupted.log`. The initial complete run had passed all 32 liveness tests. Synthetic state stopped changing at 14:34:09 UTC. Remaining files resumed individually with fresh `resumed-NN-STEM` basetemps, prioritizing the required files and new tests before optional repeats. Validation deviation: the repeat was interrupted; test bounds were unchanged.
- `git diff --check` and `python3 -m py_compile` over the changed daemon, all five new test modules and both mutation runners passed. No event/record schema key or refusal was added; existing route-event exact assertions were inspected.
- Named-file `git add subfleet/daemon.py docs/acceptance-contract.md tests/fake/test_idle_capacity_guard_news.py tests/fake/test_idle_route_deferrals.py tests/fake/test_idle_capacity_refresh_bound.py tests/fake/test_idle_capacity_pools.py tools/idle_admission_mutations.py`, then `git commit -m 'Retain admission capacity news across temporary guards and reset route errors'`, both failed with a denied `/Users/maxghenis/subfleet-v2/.git/worktrees/20261113-070659-ia-fix-r1/index.lock` (exit 128). No commit was created. The late-ramp step's named-file `git add subfleet/daemon.py tests/fake/test_idle_capacity_guard_news.py docs/acceptance-contract.md` and `git commit -m 'Preserve capacity release news across reservation-time ramp holds'` also failed at the same denied lock (exit 128).

Q8: these fixes retain the full unprepared-route evaluation required to classify the complete verdict and preserve resets. They do not remove the cost r1 measured as 109.14 ms versus 31.78 ms before change 2. On one 129-job, all-unprepared synthetic fixture with 100,000 padding events (101,181 total after warmups), two warmups and seven due profiles per source produced these medians:

| Metric | Starting HEAD | Fix r1 |
| --- | ---: | ---: |
| Profiled due-pass wall time | 2575.20 ms | 3748.59 ms |
| Process CPU for admission | 211.63 ms | 318.95 ms |
| `_route` cumulative time | 143.48 ms | 954.36 ms |
| `_route` calls per pass | 129 | 129 |

Host contention varied sharply: the preliminary baseline ranged from 637.80 to 5453.74 ms, and the final source from 1924.77 to 22139.80 ms; an earlier final run had a 516.33 ms wall median and 193.88 ms CPU median. These numbers cannot resolve a causal effect on r1's original absolute timing. The full routing work remains; the added late-ramp branch is unreachable in this all-unprepared fixture. The latest measured daemon hash is `c8f09aba311b5690cea2c85fb8f6dc868d6c1bb149b475f139fbedfa28787583`, matching the final source. `fix-r1-unprepared-profile.json` records every sample, exact command, source hash and fixture detail; `fix-r1-profile-unprepared.py` preserves the executed profiler. Baseline source came from the assigned commit's Git objects and shared only unchanged sibling modules. A redundant broad archive lost its tool session identifier; after its absolute-path extraction began, its scratch destination was renamed and replaced with a regular-file blocker to prevent further extraction there. The attempted own-job `pgrep` failed with exit 3 because process enumeration was unavailable; all other known benchmark sessions completed.

Overlap remains in the ordinary/unprepared admission-wait area also touched by #295; no unrun-sweep, `_batches` or `_record_owned` lines changed.

Mutations ran only in isolated source copies through `tools/idle_admission_r1_mutations.py`, invoked as `TMPDIR=/private/tmp/sf-ia-f1/test-tmp UV_CACHE_DIR=/private/tmp/sf-ia-f1/test-tmp/uv-cache uv --cache-dir /private/tmp/sf-ia-f1/test-tmp/uv-cache run --no-sync --with 'pytest-xdist>=3.6' python tools/idle_admission_r1_mutations.py`. The initial frozen baseline passed 101 tests, one file at a time. All six requested mutant types were killed by assertions, with no setup or collection errors. The first mutant was then expanded with `--only acknowledge-news-before-waiter-checks --skip-baseline --output /private/tmp/sf-ia-f1/expanded-news-output`: all three long-guard cases failed and all six short-clock controls passed. This also verifies the corrected ramp fixture against the unfixed behavior. After the late-ramp fix, the complete baseline passed 102 tests and all six mandatory mutants were killed again: 17 selected cases, 11 assertion failures, six passing controls and zero setup/collection errors. The final runner used `--output /private/tmp/sf-ia-f1/mutations-final-output`; both final drivers exited zero.

| Mutant | Killer |
| --- | --- |
| Early census acknowledgement loses skipped-waiter news again | `test_release_news_survives_temporary_door_hold[long-machine]`, `[long-disk]`, `[long-ramp]`; six 30-second controls pass. |
| Retain prior unprepared route errors after success | Both `test_successful_route_resets_prior_error_count[True]` and `test_intervening_success_should_not_delay_start_after_release[True]`. |
| Clear saved news after the guard clears, before consumption | `test_release_news_survives_temporary_door_hold[long-machine]`. |
| Read expiries after the pass | `test_closure_expiring_during_the_first_pass_is_not_lost` and `test_closure_expiring_during_a_slow_pass_survives_its_completion`. |
| Share detached and turn censuses | `test_capacity_news_censuses_are_independent_for_each_pool`, both pool orders. |
| Drop unprepared known-reset clipping | `test_c6_10_known_operator_reset_still_beats_the_long_clock[True]`; the prepared control passes in the baseline. |

The chosen fix keeps early pool acknowledgement and saves per-waiter delivery first; mutant 1 therefore removes that delivery to restore the original fault. Each mutant ran alone and its edited source was restored from pristine bytes with a byte comparison. The pristine hashes also matched the tested workspace. `fix-r1-mutations.json` and the adjacent logs preserve exact commands, source hashes, baseline counts, killing assertions, the complete initial ledger, final baseline and six final mutation runs, the expanded first run, and restoration results. The older mutation runner's exact hurry-expression anchor was updated for the new saved-news expression.

One avoidable test-isolation deviation occurred: the scratch late-ramp reproduction used an external test path without `-p tests.conftest` or an explicit HOME override. Its fixture used a synthetic daemon root and patched Claude/desktop paths, but global safe-home initialization cannot be claimed for that run. The command was `TMPDIR=/private/tmp/sf-ia-f1/test-tmp UV_CACHE_DIR=/private/tmp/sf-ia-f1/test-tmp/uv-cache uv --cache-dir /private/tmp/sf-ia-f1/test-tmp/uv-cache run --no-sync pytest /private/tmp/sf-ia-f1/test_late_ramp_probe.py --basetemp /private/tmp/sf-ia-f1/late-ramp-probe --junitxml /private/tmp/sf-ia-f1/late-ramp-probe.xml -q` (one assertion failure). Its source and JUnit evidence are preserved as `fix-r1-late-ramp-probe.txt`/`.xml`. The permanent test subsequently ran from `tests/fake` with normal fixtures. An additional isolated-copy inverse check removed only the two late-ramp restoration lines: exactly one assertion failure, zero setup/collection errors. Its `--trace-config` log explicitly confirms registration of the copied `tests/conftest.py`; pristine daemon bytes were restored and compared. This replaces the scratch result as regression evidence.

Final-source repeat: **222 passed, 2 failed, 0 errors, 1 xfailed** across six completed file invocations. This repeat was incomplete; the initial complete 25-file validation remains **696 passed, 5 skipped, 1 xfailed**. Files ran separately with at most four workers. No inherited file-transport fixture failure occurred. No test bound changed; the whole suite was not run.

| File | Passed | Failed | Other | Seconds |
| --- | ---: | ---: | --- | ---: |
| `tests/fake/test_capacity_wait_backoff.py` | 30 | 0 | — | 91.06 |
| `tests/fake/test_admission_latency.py` | 14 | 0 | — | 354.77 |
| `tests/fake/test_admission_properties.py` | 27 | 0 | 1 xfailed | 71.31 |
| `tests/fake/test_admission_stateful.py` | 17 | 0 | — | 1445.81 |
| `tests/fake/test_admission_visibility.py` | 43 | 0 | — | 74.60 |
| `tests/fake/test_admission_route_isolation.py` | 91 | 2 | — | 641.43 |

The two repeated route-isolation failures were `test_c6_3_a_reading_that_ages_out_before_the_reservation_is_judged_again` and `test_c6_3_an_override_that_ends_before_the_reservation_is_judged_again`: their two-second freshness windows had expired before the early evaluation. This file passed all 93 tests initially in 59.13 s; the repeated file took 641.43 s. The new reservation-time ramp branch is disabled in its fixtures. A sequential focused retry preserved every bound: **1 passed, 1 failed, 0 errors in 89.79 s**; the override case again expired before the early evaluation. A clean complete final-source rerun was not obtained. The full traces and exact commands are retained in `fix-r1-route-repeat.log`/`.xml` and `fix-r1-route-timing-retry.json`/`.log`/`.xml`.

Sequential retry command: `uv --cache-dir /private/tmp/sf-ia-f1/test-tmp/uv-cache run --no-sync pytest -q --tb=short tests/fake/test_admission_route_isolation.py::test_c6_3_a_reading_that_ages_out_before_the_reservation_is_judged_again tests/fake/test_admission_route_isolation.py::test_c6_3_an_override_that_ends_before_the_reservation_is_judged_again --basetemp /private/tmp/sf-ia-f1/route-timing-retry-01 --junitxml /Users/maxghenis/.subfleet/worktrees/20261113-070659-ia-fix-r1/docs/reports/2026-11-07-idle-admission-data/fix-r1-route-timing-retry.xml`.

Repeated files interrupted for the hard time budget: `tests/fake/test_admission_liveness.py` (runner exit 130): Stopped the repeated run to preserve the three-hour deadline; initial complete run passed 32 tests; `tests/fake/test_task_hold_admission.py` (runner exit 130): Stopped the repeated file list to diagnose the two route-isolation freshness failures before the hard deadline. Their partial progress is excluded from the completed-test totals.

Each completed repeated-file command used the exported fallback TMPDIR/cache, `uv --cache-dir /private/tmp/sf-ia-f1/test-tmp/uv-cache run --no-sync --with 'pytest-xdist>=3.6' pytest -n 4 -q --tb=short FILE --basetemp /private/tmp/sf-ia-f1/PHASE-NN-STEM --junitxml /private/tmp/sf-ia-f1/PHASE-NN-STEM.xml`, with `PHASE` equal to `verified` or `resumed`. `fix-r1-validation.json` records every exact argv, count, duration and failure.

Not reached by the final full-list repeat within the remaining budget: `tests/fake/test_admission_disk.py`, `tests/fake/test_admission_ramp.py`, `tests/fake/test_idle_admission_pacing.py`, `tests/fake/test_idle_capacity_events.py`, `tests/fake/test_idle_capacity_guard_news.py`, `tests/fake/test_idle_capacity_pools.py`, `tests/fake/test_idle_capacity_queue_news.py`, `tests/fake/test_idle_capacity_refresh_bound.py`, `tests/fake/test_idle_route_deferrals.py`, `tests/fake/test_probe_cancel.py`, `tests/fake/test_reset_credit_incident.py`, `tests/fake/test_state_contract.py`, `tests/fake/test_unadmittable_pin_review.py`, `tests/fake/test_unrun_worktrees.py`, `tests/unit/test_admission_store_wakes.py`, `tests/unit/test_daemon_verbs.py`, `tests/unit/test_invariants_index.py`, `tests/unit/test_test_module_names.py`. Their earlier passing checks remain recorded above; the final mutation baseline also reran the guard, route-deferral, pool, pacing and capacity-event modules, and the focused late-ramp guard run passed all 16 tests.

Final compilation of the daemon, five new test modules and both mutation runners, `git diff --check`, and independent mutation-ledger/source-hash assertions passed. `fix-r1-final-checks.json` records these commands and confirms the final daemon hash. The independent source review found no further defects.

Cleanup: the 13 generated directories outside scratch were these paths under `/Users/maxghenis/.subfleet/worktrees/20261113-070659-ia-fix-r1`: `.venv`, `.pytest_cache`, `.hypothesis`, `tools/__pycache__`, `subfleet/__pycache__`, `subfleet/adapters/__pycache__`, `subfleet/sessions/__pycache__`, `subfleet/conversations/__pycache__`, `subfleet/gate/__pycache__`, `subfleet/guard/__pycache__`, `tests/__pycache__`, `tests/unit/__pycache__`, `tests/fake/__pycache__`. Their full absolute paths were recorded before deletion in `fix-r1-cleanup.json`. Each was moved under `/private/tmp/sf-ia-f1/workspace-generated`, then deleted with `shutil.rmtree` after asserting `sf-ia-f1` was in its destination path. All 13 were removed. The final guarded deletion of the scratch tree was interrupted at 15:08 UTC (exit 130) to preserve the deadline; remaining scratch files stay under `/private/tmp/sf-ia-f1`. The cleanup manifest records the interruption. The aborted archive was unchanged since its recorded observation. Its lost tool session identifier remains the deviation described above. Owned test and cleanup runners completed or were interrupted; mutation and known profiling sessions completed.

Report completed 2026-11-13 15:09:23 UTC, within the three-hour limit. Git refused the step commits at `index.lock`; fixes and evidence remain in this workspace for collection. Nothing was pushed.

Final delivery attempt: `git add --` with 50 explicitly named files, then `git commit -m 'Fix idle admission review r1 and record validation'`, both exited 128 at the same denied worktree `index.lock`. Exact named-file argv and errors are in `fix-r1-final-checks.json`; no commit was created.

Head built: `9e753f8fb7eff14f2cbabb5e871b4b38ed509a96`
