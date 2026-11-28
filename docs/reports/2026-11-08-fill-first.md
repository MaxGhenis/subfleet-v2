# Claude fill-first ranking (D-FILL1)

Implemented from `266c3ab8b0be2548fbe0abc77273fa1ccd7fa9a9`, in the assigned isolated workspace. The sandbox denies the shared Git `index.lock`: every named-file staging attempt failed, so no commit or branch switch could be made. The files remain available for Subfleet salvage. Nothing was pushed, no history was rewritten, and no caller worktree file or live state was read or changed.

## Order and policy

`admission.lane_order` ships as `{"claude": "reset", "codex": "reset"}`. “Why reset ships” explains the default. An omitted provider in an explicit map uses `reset`. `admission.expiring_s` accepts positive whole seconds or null, defaults to 21600, and null disables expiry preference. `admission.lane_spread` accepts its existing positive integer/null scalar or a provider map; omitted map providers use 2. The default remains 2. Validation names the offending key, including provider entries.

Both orders share one prefix: desktop last, a turn's affinity first in its desktop class, load band, then Claude stranded capacity. The separate desktop-org build can insert `desktop_possible` immediately after desktop in that one prefix. Fill then orders weekly reserve false first, five-hour reserve false first, expiring true first, expiring reset ascending, weekly use descending, in-flight ascending, lane id. All non-expiring lanes share the reset-tier value.

Fill selects the latest numeric provider/stale-provider observation no later than now for each applicable scope/window, then retains it only when its known reset is strictly future. Reading age does not discard open windows. Weekly use is the maximum retained account/requested-model weekly utilization, binding the earliest reset then scope on a tie; missing use is zero. Five-hour use follows the same rule. Simultaneous observations select greater use then earlier reset, independently of input order. Fractional reset precision is preserved with exact datetime comparisons, including simultaneous observations in distant windows. Reserves compare utilization directly against the complement, strictly above it.

Separate fill evidence survives store/view compression when a newer future, unknown or null row hides an older provider observation. It never supplies admission freshness. Eligibility, refusals, floors, probes, pick exclusions and C-11.7 keep their predicates. `ranking_usage` remains unchanged; fill adds `lane_order`, `fill_weekly_used`, `fill_weekly_reset`, `fill_five_hour_used`, `fill_expiring` beside its old fields. These appear in `why`, `pick --all`, and fill decision reasons. Unknown-model pick aggregates fill evidence across every configured model it must qualify for.

Reservation revalidation retains unchanged verdicts and rejudges at new evidence, future observation, expiry-tier entry or open-window reset, including stale observations. The daemon passes the configured policy to per-lane horizons; override expiry restores auxiliary evidence, and deletion checks cover it. Exact-comparison searches covered every new decision key and candidate/rejection/evaluation comparisons. Historical reset reference tests now select reset explicitly. C-11.3 is documented as reset; C-11.11 and two change-log entries were added without a PR number. C-11.7 and C-23.37 cross-references now name the configured order.

## Export measurement and replay

The replay used a copy of the supplied offline export, never a live store. The copy is 55,074,816 bytes, SHA-256 `d32bef807da530aca0ff3cca9cc9161a089b2dedbe0f6066da89156c958eff06`. SQLite opened only that copy with `mode=ro` and `query_only=ON`. The raw result is [2026-11-08-fill-first-replay.json](2026-11-08-fill-first-replay.json). Every account label in the output is a lane id; account identity keys remain internal and the serialized report contains no email markers.

The replay covers **2026-10-31T23:51:40Z through 2026-11-07T23:51:40Z**. It reconstructs all 309 recorded attempt admission snapshots (85 Claude, 224 Codex), counts 473 recorded job arrivals and 287 first attempts, and loads 20,814 initialization/replay readings. Claude contributes 157.133729 attempt-hours. Recorded retry arrivals, models, durations, standing candidate sets and closures remain fixed. There are no turn attempts in this interval. Historical ramp closures therefore preserve much of the observed spreading: the replay cannot predict the hub's separate ramp change.

Only Claude's lane order and load band change between variants. Codex remains on `reset`, spread 2, and its per-provider results are identical in all six runs. The spread-2/21600 case serves both requested groups of variants and is shown once.

| Claude placement rule | Unused weekly capacity sum | Modeled limited attempts | Accounts taking work | Top 1 hour share | Top 2 | Top 3 | Hourly mean partial accounts | Hourly mean whole accounts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Today's reset, spread 2 | 2.141760 | 7/85 (8.24%) | 7 | 28.53% | 46.93% | 63.76% | 4.619 | 2.196 |
| Fill, spread 2, expiring 21600 s | 2.379958 | 15/85 (17.65%) | 7 | 24.93% | 44.16% | 60.98% | 4.292 | 2.196 |
| Fill, spread 4, expiring 21600 s | 2.319958 | 9/85 (10.59%) | 7 | 24.93% | 44.16% | 60.98% | 4.292 | 2.196 |
| Fill, spread null, expiring 21600 s | 2.319958 | 9/85 (10.59%) | 7 | 24.93% | 44.16% | 60.98% | 4.292 | 2.196 |
| Fill, spread 2, expiring null | 2.379958 | 15/85 (17.65%) | 7 | 24.93% | 44.16% | 60.98% | 4.292 | 2.196 |
| Fill, spread 2, expiring 86400 s | 2.379958 | 15/85 (17.65%) | 7 | 24.93% | 44.16% | 60.98% | 4.292 | 2.196 |

Unused sum is normalized account-week capacity across eight observed Claude account resets, not tokens or money. There are six observed Codex resets too; their unused sum is 0.2 in every run. Shares group aliases by account and sum recorded attempt durations clipped at the replay end. Accounts taking work counts distinct account groups receiving at least one attempt. The hourly metric samples `since + h hours`, `h=0..167`, after readings, resets and admissions at the same instant, across all eight enabled Claude account groups, including temporarily closed ones. It uses the latest account `seven_day` physical modeled utilization; unknown or expired windows count as zero. Partial includes both 10% and 90%; whole means below 10%. Counting unknown accounts as whole can overstate untouched capacity. The weekly model-binding window used to rank a particular request remains separate from this account metric.

**The model is uncalibrated.** It predicts 7/85 Claude limits under today's rule against 3/85 actually recorded; Codex predicts 2/224 against 0/224 recorded. Across both providers it predicts 9/309 against 3/309 recorded, and moves 81/309 placements off their factual lane. Its cost attribution spreads positive utilization deltas over overlapping active seconds, which can misattribute unrelated desktop work, and imputes unobserved/limited demand from successful siblings or provider/window medians. All 85 Claude five-hour costs lack a successful observed cost pool and are imputed as zero, so this replay cannot estimate five-hour reserve efficacy. Ranked utilization changes only at factual observation timestamps; job admissions change the physical cost model immediately. Pre-reset observations range from 7 to 508,568 seconds old across both providers. The results do not establish a production limit rate or show improved top-account concentration. The wider-band variants reduce modeled limits relative to fill spread 2 in this frozen workload, but the model does not justify changing the shipped spread default.

## Measured monotonicity

The audit reads numeric `provider` or `stale-provider` `seven_day` and `five_hour` observations, ordered by observation time and reading id. It compares consecutive rows of the same lane, scope and window only when their reset timestamps are identical and non-null and both observations are before that reset. It compares utilization exactly, with no tolerance or rounding. Changed reset clocks begin a new window for this measurement, including small Codex clock jitter.

The full export has 41,803 numeric rows from **2026-10-24T00:05:21Z through 2026-11-08T00:07:15Z**. Of **40,972 consecutive open-window pairs**, **zero decrease**: 704 Claude weekly pairs, 278 Claude five-hour pairs and 39,990 Codex weekly pairs. Restricting evidence to the replay end yields 41,759 rows, 40,930 qualifying pairs (702 Claude weekly, 278 Claude five-hour, 39,950 Codex weekly) and zero decreases. The design record's 41,399 count was not reproduced under this exact window definition; it is not quoted as a new measurement. If equal-reset pairs observed after reset are included, the full-export count is 40,973, still zero decreases; they are excluded from the stated open-window evidence.


## Invariants and tests

`tests/unit/test_scheduler_fill.py` contains 43 cases with 4,500 configured Hypothesis examples. The frozen full base evaluator permits complete decision/error comparisons without Git history at test time. Its SHA-256 is `f6c9ad0cae11fd8b9d624f856b0782ddb7e155cbc4dfca1ab85ce141d1da7d54`; extracted `rank_key` is `1e100b7a9681f7bf7dc19ebf5d8e701785ce3fb8d5d0d57d5041e45f88bc6a54`, and `ranking_usage` is `ade25eea6e4bdbed6f569d7812bafbd64c1c3a093bbf87341d01d338083a7e21`. The frozen bytes were checked against `git show 266c3ab8b0be:subfleet/scheduler.py` while building. Both evaluators receive the same policy, view and job, including the input policy hash; changed default-policy bytes naturally have a different hash.

| Invariant | Test in `test_scheduler_fill.py` |
| --- | --- |
| Same candidates and exact refusal predicates, only ranking changes | `test_fill_and_reset_have_identical_candidates_and_refusals`; `test_fill_complete_chain_has_identical_candidates_and_rejection_predicates`; `test_fill_keeps_the_same_probe_predicate_for_each_pinned_lane` |
| Complete reset decisions and errors equal the frozen base, including both providers' defaults | `test_reset_decisions_equal_frozen_release217`; `test_reset_complete_chain_decisions_and_errors_equal_frozen_release217`; `test_default_decisions_equal_frozen_release217` (shipped and omitted defaults); `test_frozen_release217_ranking_sources_have_verified_sha256` |
| Desktop last; affinity leads its class | `test_fill_desktop_last_and_turn_affinity_leads_its_class` |
| Higher weekly use cannot rank later with all earlier tiers equal | `test_fill_more_weekly_use_never_ranks_after_less_in_equal_earlier_tiers` |
| Only latest applicable open windows contribute, otherwise zero | `test_fill_only_latest_applicable_open_windows_contribute_at_any_age`; `test_fill_keeps_old_open_provider_reading_and_ignores_expired_one` |
| Expiring first, earlier expiry first; null disables | `test_fill_expiring_precedes_nonexpiring_and_earlier_expiring_reset_wins`; `test_fill_null_expiring_disables_the_tier`; `test_fill_expiring_reset_order_keeps_fractional_seconds` |
| Unique total keys; lane/reading permutations cannot change order | `test_fill_keys_are_unique_total_and_order_independent`; `test_fill_equal_observation_ties_do_not_depend_on_reading_order` |
| Sequential packing | `test_fill_packs_a_prefix_of_starting_use_when_expiry_and_bands_are_off` |

The precise packing claim: 2–7 initially eligible Claude lanes, initial weekly use from 0 through 0.9 (ties allowed, lane id breaks ties), identical still-open weekly resets, five-hour use 0.5, and equal desktop/affinity/stranded keys. The band and expiring tier are disabled. Each of 1–50 sequential jobs adds a fixed 0.001–0.3 weekly use, capped at 1; no window resets during the simulation. At every step, lanes ever selected form an initial segment of the lanes sorted by (-starting weekly use, lane id). Before a fresher lane first receives work, every preceding lane is either currently refused at exhaustion or strictly above 0.98 weekly use. The property does not assert a prefix across different earlier keys or expiry priorities, or arbitrary external refusals.

Additional generated tests cover bounded store candidates versus full histories (100 examples), cached-route expiry/reset equality (200 examples), and policy values (150 examples). Explicit tests cover both reserve complements at equality and one float above, per-provider bands, stranded precedence, auxiliary evidence masks, noncanonical/future timestamps, override restoration, isolated default maps, conservative pick freshness, unknown-model pick scope and CLI output.

## Mutants

`tools/fill_rank_mutations.py` applied each mutant alone to a pristine file copy, ran its named test, restored the original bytes in `finally`, and verified SHA-256. No Git checkout was needed. The first build's six mutants failed by assertion. “Default fix r2” records eight assertion kills, “Review fix r1” records thirteen, and “Review fix r2” records seventeen. Evidence: [2026-11-08-fill-first-mutants.json](2026-11-08-fill-first-mutants.json).

| Mutant | Killing test in `test_scheduler_fill.py` |
| --- | --- |
| Weekly use ascending | `test_fill_more_weekly_use_never_ranks_after_less_in_equal_earlier_tiers` |
| Only readings under the 120-second TTL | `test_fill_keeps_old_open_provider_reading_and_ignores_expired_one[121]` |
| Expired window still counts | `test_fill_keeps_old_open_provider_reading_and_ignores_expired_one[121]` |
| Expiring tier dropped | `test_fill_expiring_precedes_nonexpiring_and_earlier_expiring_reset_wins` |
| Desktop dropped from fill | `test_fill_desktop_last_and_turn_affinity_leads_its_class[claude-opus]` |
| Default Codex follows fill | `test_default_decisions_equal_frozen_release217[codex-astra-omitted]` |

## Verification and commands

Final checks ran in the requested order, using the permitted child of TMPDIR, its uv cache, fresh basetemp directories and at most four xdist workers. `uv sync --group dev` passed after the default cache was denied and moved into that child. Commands and results, including source reads, failed setup/test attempts, replay audits and staging attempts, are recorded in [2026-11-08-fill-first-commands.md](2026-11-08-fill-first-commands.md).

The pytest prefix was `uv --cache-dir "$UV_CACHE_DIR" run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4`, with `-ra` for the final regression/contract runs and a new `--basetemp` each time.

| Final check | Command target | Result |
| --- | --- | --- |
| New tests | `tests/unit/test_scheduler_fill.py tests/unit/test_policy_fill.py tests/unit/test_fill_routing.py tests/unit/test_fill_evidence.py tests/unit/test_replay_weekly_fill.py` | 121 passed, 5.58 s |
| Mutants | `python tools/fill_rank_mutations.py --basetemp <fresh> --output docs/reports/2026-11-08-fill-first-mutants.json` | 6/6 assertion kills; restored bytes verified |
| Scheduler, every caller/mention of rank_key/ranking_usage/evaluate, policy tests and CLI/picker/capacity | `@docs/reports/2026-11-08-fill-first-test-files.txt` (66 files) | 2,651 passed, 2 skipped, 231.78 s |
| Final contract checks | `tests/fake/test_state_contract.py tests/unit/test_test_module_names.py tests/unit/test_invariants_index.py tests/unit/test_handler_refusals.py tests/unit/test_protocol_registry.py` | 427 passed, 20.01 s |
| Replay verification | Four complete offline replays, JSON equality/accounting/privacy assertions, 20 dedicated replay tests | Identical variant metrics; Codex unchanged; passed |
| Static/source checks | `python3 -m py_compile`, `git diff --check`, SHA-256 assertions | Passed; current ranking_usage source matches frozen base |

The first 65-file regression run had 2,611 passed, 12 failed and 2 skipped. Failures identified historical reset expectations, older frozen evaluator assumptions and two pinned default-policy hashes. Those tests now explicitly select reset or pin the updated policy bytes; their 10-file rerun passed 356 tests. Final review also replaced float epoch tie comparisons with exact datetime arithmetic and added a distant-window microsecond witness before repeating the ordered checks above. No test predicate was weakened to hide a production failure.

The two skips are existing importer real-daemon cases at `tests/unit/test_importer.py:1693` and `:2642`: macOS boot identity is unavailable because the sandbox denies required sysctl/process inspection. No requested non-skipped test remains failing. The separate full suite was not run, as instructed; CI owns it.

The added fill store accessor took 0.042736 s median versus 0.029042 s for the existing latest accessor over five read-only calls on an export copy (66 versus 138 rows returned). SQLite inlined the provider CTE; this small snapshot benchmark does not establish production latency.

## Limits and delivery

The full suite is reserved for CI. No live daemon, provider API, native app or live store was used; tests used temporary isolated state and fake adapters. The separate desktop-org and ramp changes are outside this build. Replay conclusions are limited as stated above. Git staging/commits and the branch switch are blocked by the denied shared index lock; no private Git metadata workaround was used.

Head: `266c3ab8b0be2548fbe0abc77273fa1ccd7fa9a9`

## CI fix r1 (2026-11-13)

### Cause

`tests/unit/test_store_contention_queries.py::test_a_view_from_the_store_matches_one_from_every_reading` (C-3.7) failed on both Pythons. At `0e5edeba5`, `capacity.store_rows` took `Store.fill_reading_candidates(now=now)` when the store had it and fell back to the ordinary reading candidates otherwise. `build_view` copied its input into `view["fill_readings"]`. The SQL dropped each key's canonical provider history before its latest observation, while a view built from every reading kept that history. The two views held different raw rows. The reproduction yielded 1 failure and 211 passes.

### Consumer audit

Every reader of `fill_readings` in `subfleet/` and `tools/` (`grep -rln "fill_readings\|fill_reading_candidates\|fill_candidates" subfleet tools app bin`: store, picker, capacity, scheduler, daemon, route_check):

| Reader | What it reads, at which clock | Rows that are neither the latest numeric provider observation at or before that clock, nor after it, change its result? |
| --- | --- | --- |
| `scheduler.evaluate` → `judge_lane` → `fill_ranking_usage` (`scheduler.py:1399`, `:1048`, `:971`) | `view["fill_readings"]` by lane and scope, at `view["now"]`, the view's clock in whole seconds | No. Per scope and window it takes the latest numeric provider row at or before its clock, then checks its reset. Every tie at that instant and every later row is kept. Fully tied rows give the same output fields. |
| `picker.rank` (`picker.py:139`) | `fill_ranking_usage` at `view["now"]` | No, for the same reason. |
| `route_check.still_stands` (`route_check.py:187`) | `fill_candidates`, which the daemon passes as the raw `basis["rows"]["view"]["fill_readings"]`, at the reservation's clock, never earlier than the view's | Not affected: it reads the store's raw rows, not the view's. With no `fill_candidates`, it reads the view's rows at a later clock, and the later rows are kept. |
| `Daemon._capacity_rows` (`daemon.py:2280`) | Reads `fill_reading_candidates()` at the snapshot's `utc_now()` when a configured model's provider uses fill | Supplies `fill_readings: []` when every configured model uses reset. |
| `Daemon._pick` (`daemon.py:2832`) | drops overridden lanes' rows | Dropping by lane commutes with a reduction by lane, scope and window. |
| `Daemon._route_rows` (`daemon.py:9407–9408`) | reading ids of the raw rows in `rows["view"]` | Not affected: raw rows. |
| `capacity.lane_horizons` (`capacity.py:319`) | For each retained fill-order provider row, notes a future `observed_at`, or the open window's reset and any upcoming expiring tier. `Daemon._pick` (`daemon.py:2837`) is its only production caller. | **Yes.** A replaced observation can still move the horizon earlier when it has a noncanonical timestamp or when the replacement arrived between the store snapshot and the view's clock. `build_view` runs `fill_candidates`, which drops a replaced canonical row when the replacement is present in the snapshot and observable at the view's clock. Removing a redundant clock can lower the `rejudged` route counter. `test_decision_horizon.py` checks decision stability up to each horizon. |
| `tools/replay_weekly_ranking.py` | its own SQL and its own fill selection (`fill_latest`) | Never reads a view. |
| `render.status`, `policy` compatibility entry (`policy.py:1051`), `Actions` (`actions.py:822`), `Timers.snapshot` | Build views from given rows. The timer reads fill rows only when a configured model's provider uses fill. Only the policy entry calls `evaluate`. | The policy entry's evaluation follows the rule above. No path calls `lane_horizons`. `status.json` omits `fill_readings`. |

So the reduction the brief prefers, to exactly the rows that can bind fill by parsed time, gives `capacity.lane_horizons` a different result whenever the snapshot holds a replaced provider row with an open window. The query can keep such a row when its timestamp is noncanonical. Before this fix, a view built from every reading kept all replaced rows. I didn't check which writers store noncanonical timestamps: the importer and the adapters pass `observed_at` through from their callers. As the brief directs, I did not apply that reduction.

### Fix

`capacity.fill_candidates(readings, now=)` applies `Store.fill_reading_candidates`' own rule in Python, and `build_view` runs it on the fill rows it is given (`fill_readings`, else `readings`), at the view's clock in whole seconds:
- it keeps `provider` and `stale-provider` rows with finite numeric utilization;
- per lane, scope and window, it keeps the ties at the greatest canonical timestamp at or before that clock;
- it keeps every later canonical row and every row with another timestamp shape;
- rows stay in their input order.

The query's rows come back from it unchanged. Views built from the store therefore carry exactly what they carried before, and every reader above returns the same result, `lane_horizons` included. A view built from every reading now carries the same rows. That changes nothing for the callers that build views from every reading: render and status don't read fill rows, and the policy entry's `evaluate` reads only the latest rows.

One case remains. The daemon reads the query at the snapshot's second and builds the view at a later instant. If a reading stamped after the snapshot's second becomes current before the view's second, the view drops the replaced rows that the query still held. That needs a reading stamped in the future, and its only effect is the same spurious-clock removal described for `lane_horizons`.

To switch to the parsed-time reduction, replace the canonical-string comparison in `fill_candidates` with a comparison of parsed times. The cost is the `lane_horizons` difference in the table, and the decision on that is Max's. The test assertion is unchanged.

Commits: `334a91962` (fix), `6b76c4203` (tests), on `fix/fill-first-ci-r1` from 0e5edeba5.

### Property test and mutant

- `tests/unit/test_fill_evidence.py::test_fill_rows_chosen_from_the_store_query_match_those_from_every_reading` uses Hypothesis with 100 derandomized examples, each on a real `Store`. Each example generates up to 16 readings over two lanes, two scopes and two windows:
  - canonical, `+00:00`, fractional and `+02:00` timestamps;
  - rows from 2 hours before the view's clock to 10 minutes after it;
  - a view clock with and without a fraction;
  - null, 0, 0.2, 0.8, 1, -0.5, infinite and text utilization (the last three inserted under `PRAGMA ignore_check_constraints`);
  - every label plus an unknown one;
  - no reset, a past reset or a future reset.

  It asserts three things:
  - `fill_candidates(query rows) == fill_candidates(every row)`;
  - `fill_candidates(query rows) == query rows`;
  - per lane, `fill_ranking_usage` on the chosen rows equals it on every row at the view's clock and at +1 s, +30 s, +600 s and +50,000 s.
- `test_a_view_from_every_reading_keeps_the_rows_a_later_clock_reads` builds a view from every reading with two future rows, checks that it equals the store's view, and checks that fill's weekly use reads 0.5, 0.2 and 0.3 at +0 s, +10 s and +20 s.
- Mutant: keep only rows observed at or before the clock (drop canonical rows after it, and noncanonical rows whose parsed time is after it), applied to a copy in scratch. Both new tests kill it: 2 failed, 14 passed. The original view test passes under it, because both of its sides lose the same rows.

### Commands and results

Setup: `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)` and `UV_CACHE_DIR=$TMPDIR/uv-cache`, then `uv sync --group dev`, which passed. `uv run --no-sync --with 'pytest-xdist>=3.6'` then failed with `unrecognized arguments: -n`. The shared cache's xdist archive has lost its `METADATA` and `entry_points.txt` (its dist-info holds only `licenses/`), so pytest never sees the plugin. Every xdist run below therefore used `uv run --no-sync --cache-dir $TMPDIR/sf-ff-ci1/uv-cache --with 'pytest-xdist>=3.6' python -m pytest <file> -n 4 --basetemp $TMPDIR/sf-ff-ci1/bt-<n> -q -p no:cacheprovider`. Small files ran serially with `.venv/bin/python -m pytest` and their own basetemp. Each file ran alone.

| Run | Target | Result |
| --- | --- | --- |
| bt-01, at 0e5edeba5 | `tests/unit/test_store_contention_queries.py` | 1 failed (the view test), 211 passed, 224 s |
| bt-02, fix only | that view test plus `tests/unit/test_fill_evidence.py` | 14 passed |
| bt-03, fix and new tests | `tests/unit/test_fill_evidence.py` | 15 passed, 98 s |
| bt-mut1, mutant copy | `tests/unit/test_fill_evidence.py` plus the view test | 2 failed (both new tests), 14 passed |
| bt-11 | `tests/unit/test_store_contention_queries.py` | 212 passed, 212 s |
| bt-12 | `tests/unit/test_invariants_index.py` | 25 passed |
| bt-21 | `tests/unit/test_decision_horizon.py` | 7 passed, 129 s |
| bt-22 | `tests/unit/test_route_check.py` (`-n 4`, Hypothesis with 1,500 examples) | Stopped unfinished after about 12 min, with no result; the host's load average was 97 |
| bt-41 | `tests/unit/test_scheduler_fill.py` (serial) | 38 passed, 396 s |
| bt-42 | `tests/unit/test_fill_routing.py` (serial) | 7 passed |
| bt-43 | `tests/unit/test_policy_fill.py` (serial) | 43 passed |
| bt-51 | `tests/fake/test_state_contract.py` | 37 passed, 104 s |
| bt-61 | `tests/unit/test_replay_weekly_fill.py` (serial) | 20 passed |
| bt-62 | `tests/unit/test_scheduler_weekly.py` (serial) | 33 passed, 342 s |

Under the 2-hour limit and that load, these test files the PR changes were not run locally:
- `test_route_check.py` (stopped, as above);
- `test_scheduler.py`, `test_scheduler_split.py`, `test_scheduler_tier_preferences.py`;
- `test_provider_active_cap.py`, `test_policy.py`, `test_policy_ramp.py`;
- `tests/fake/test_review_needs_shell.py`.

CI runs them. No run failed after the fix.

Other commands:
- `git diff --name-only origin/release/217...HEAD` listed the PR's files.
- Greps over `subfleet` and `tools` drove the consumer audit.
- `date -u` gave the start time, 21:53:28Z.

Deviations from the brief:
- The view applies the query's rule, not the parsed-time reduction; the audit above says why.
- xdist ran from a scratch uv cache, because the shared cache's xdist archive is damaged.
- Some test files ran serially, without xdist.
- The work is on a new branch, `fix/fill-first-ci-r1`, because the checkout was detached; nothing was pushed.

## Default fix r2 (2026-11-25)

Round 1 was cancelled on 2026-11-17 before it wrote a commit or report. This round reran it from that round's uncommitted draft, saved as `4066743d9`.

### Setup and pristine fill probe

`date -u` started the clock at 21:26:50Z. `git rev-parse HEAD` returned `42da87eb5291e97948776721e153f4f63312bbb1` and `git status --porcelain` was empty. Read the preceding report, the saved replay JSON, C-11.3, C-11.7, C-11.11, C-23.37, both D-FILL1 change-log lines, and the PR body with `gh pr view 312 --repo MaxGhenis/subfleet-v2`. Read the draft with `git diff 42da87eb5 4066743d9 -- <path>` and the PR's own changes with `git diff 4aed7a3c9 42da87eb5 -- <path>`.

The sandbox denied `mkdir "$(getconf DARWIN_USER_TEMP_DIR)/sf-fill-def-r2"` and `uv sync --group dev` could not initialize its shared cache. I created `/private/tmp/sf-fill-def-r2` and ran `python3 -m venv --without-pip .venv`. I copied locked dev packages and xdist 3.8.0 with execnet 2.1.2 from the shared cache. The cached Hypothesis 6.168.1 was built for free-threaded Python, and its import failed under standard Python 3.14.7. I downloaded the 784,662-byte wheel named in `uv.lock` with `curl --fail --silent --show-error --limit-rate 300k`, verified SHA-256 `48e467c73b7fb46bc072323c5d67e8e683cf32218fbba47c4e6ee0fe0d72687f`, and extracted it into the venv. Imports reported Hypothesis 6.168.1, pytest 9.1.1, and xdist 3.8.0. The venv took 15 MB. The shared cache was read only.

Pytest ran via `.venv/bin/python -m pytest`. Each probe interruption restored the mutated files in `finally`. The first launch was stopped before it reported tests to redirect pytest's shared failure artifacts into scratch. The second was stopped during startup tests because the picker edit contained a duplicated conditional. The driver now compiles each mutation before writing it. Those two launches used `pytest-probe-before`. The corrected launch at `pytest-probe-before-3` was stopped at 172 MB of scratch, with 595 pass marks and 69 failure marks in the partial output. I deleted those basetemps and restarted from clean HEAD.

The successful commands below set `TMPDIR` and `PYTHONPATH` to scratch. The scratch plugin points pytest's failure artifacts there and removes each test's own `tmp_path` after teardown to bound disk use. `df -H /` was checked before each run. Basetemps are under `/private/tmp/sf-fill-def-r2`, which was checked with `du -sm` throughout.


`.venv/bin/python /private/tmp/sf-fill-def-r2/probe.py before 4`. The 68-file probe finished with 349 failed, 2539 passed, 2 skipped in 1677.76s (0:27:57). 307 failing ids reached a fill branch. 0 other ids reached a branch without a failing result. Scratch was 63 MB. The driver restored and verified the SHA-256 of all three source files. The complete command, counts and ids are in [the probe JSON](2026-11-08-fill-first-default-r2-probe.json).

The importer failures came from running their synthetic conversation workspaces under `/private/tmp`. `subfleet/conversations/catalog.py:744–747` classifies those paths as temporary, so sessions are not continuable. The CLI failure was the monotonic wall-clock gap assertion in its long-poll test under load. The probe JSON records their 42 IDs separately from fill-reach failures. The 30-minute probe milestone slipped during setup retries and the loaded 68-file run. Guarded cleanup removed `pytest-probe-before-4/popen-gw0/admission0` through `admission176` after their examples finished, then removed the completed basetemp.

Save the following two files in scratch to rerun the probe from the repository root.

`sf_fill_scratch.py`

```python
import os
from pathlib import Path

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure():
    from tests import failed_keep

    failed_keep.ROOT = Path(os.environ["TMPDIR"]) / "failed-keep"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item):
    import shutil

    path = item.funcargs.get("tmp_path")
    result = yield
    if path is not None and path.is_dir():
        base = item.config._tmp_path_factory.getbasetemp()
        assert path.is_relative_to(base) and "sf-fill-def-r2" in path.parts
        shutil.rmtree(path)
    return result
```

`probe.py`

```python
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root = Path.cwd()
scratch = Path(__file__).resolve().parent
phase, run_id = sys.argv[1:]
basetemp = scratch / f"pytest-probe-{phase}-{run_id}"
output = scratch / f"probe-{phase}.log"
trace = scratch / f"reach-{phase}.txt"
assert not trace.exists()
report = root / "docs/reports/2026-11-08-fill-first-default-r2-probe.json"
targets = {
    "subfleet/scheduler.py": [
        ('    if lane_order(policy, model["provider"]) == "fill":\n', '        '),
        ('    if lane_order(setup["policy"], provider) == "fill":\n', '        '),
    ],
    "subfleet/picker.py": [
        ('        fill = (fill_ranking_usage(fill_applicable, now=now, admission=admission_settings(policy))\n',
         '            '),
    ],
    "subfleet/capacity.py": [
        ('                or not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used)):\n'
         '            continue\n', '        '),
    ],
}
pristine = {path: Path(path).read_bytes() for path in targets}
hashes = {path: hashlib.sha256(data).hexdigest() for path, data in pristine.items()}
command = [
    ".venv/bin/python", "-m", "pytest", "-q", "-n", "4", "-p", "no:cacheprovider",
    "-p", "sf_fill_scratch", "--tb=short", "-r", "fEs", "--basetemp", str(basetemp),
    "@docs/reports/2026-11-08-fill-first-test-files.txt",
    "tests/unit/test_replay_weekly_fill.py", "tests/unit/test_store_contention_queries.py",
]
disk = subprocess.check_output(["df", "-H", "/"], text=True)
print(disk, end="", flush=True)
free = subprocess.check_output(["df", "-k", "/"], text=True).splitlines()[1].split()[3]
if int(free) * 1024 < 60_000_000_000:
    sys.exit("Less than 60 GB free")
try:
    for path, edits in targets.items():
        source = pristine[path].decode()
        for marker, indent in edits:
            assert source.count(marker) == 1, (path, marker)
            line = indent + "raise FillReach()\n"
            if path.endswith("picker.py"):
                replacement = '        if lane_order(policy, family) == "fill":\n' + line + marker
            else:
                replacement = marker + line
            source = source.replace(marker, replacement)
        changed = source + r"""

class FillReach(BaseException):
    def __init__(self):
        from os import environ

        node = environ.get("PYTEST_CURRENT_TEST", "").rsplit(" (", 1)[0]
        if node:
            with open(environ["SF_FILL_REACH_LOG"], "a") as seen:
                seen.write(node + "\n")
"""
        compile(changed, path, "exec")
        Path(path).write_text(changed)
    with output.open("w") as log:
        run = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                  "PYTHONUNBUFFERED": "1", "TMPDIR": str(scratch),
                                  "PYTHONPATH": str(scratch), "SF_FILL_REACH_LOG": str(trace)})
finally:
    for path, data in pristine.items():
        Path(path).write_bytes(data)
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == hashes[path]
log = output.read_text()
failure_lines = re.findall(r"^FAILED (.+)$", log, re.MULTILINE)
failed = sorted(line.split(" - ", 1)[0] for line in failure_lines)
observed = set(trace.read_text().splitlines()) if trace.exists() else set()
direct = {line.split(" - ", 1)[0] for line in failure_lines if "FillReach" in line.split(" - ", 1)[-1]}
reached = sorted((observed & set(failed)) | direct)
summary = [line for line in log.splitlines() if re.search(r"\b(?:passed|failed|error|skipped)\b.* in [\d.]+s", line)]
assert summary and run.returncode == 1, log[-4500:]
counts = Counter({name: int(number) for number, name in
                  re.findall(r"(\d+) (passed|failed|skipped|errors?)\b", summary[-1])})
assert len(failed) == counts["failed"], (len(failed), counts)
assert not (counts["errors"] or counts["error"]), summary[-1]
payload = json.loads(report.read_text()) if report.exists() else {}
payload[phase] = {
    "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "command": command,
    "returncode": run.returncode,
    "summary": summary[-1],
    "counts": dict(counts),
    "fill_reached": len(reached),
    "fill_test_ids": reached,
    "fill_reached_without_failure_ids": sorted(observed - set(failed)),
    "other_failed_test_ids": sorted(set(failed) - set(reached)),
    "restored_sha256": hashes,
}
report.write_text(json.dumps(payload, indent=2) + "\n")
print(summary[-1], f"({len(reached)} FillReach failures)", flush=True)
```

Git refused staging the two named report files because the worktree's `index.lock` could not be created. Step 1 is recorded in the worktree for salvage.

### Default tests and shipped bytes

Applied the draft's four default-test hunks with the production defaults from `42da87eb5`. The serial red run returned 5 failed and 2 passed in 21.64 s. The renamed raw, bundled and loaded policy test, the map-copy test, and the loaded admission test each failed on the expected reset map. The frozen full-Decision law failed both Claude cases, `[claude-opus-shipped]` and `[claude-opus-omitted]`. Both Codex cases passed.

The red run used these four selectors.

- `tests/unit/test_policy_fill.py::test_reset_defaults_apply_to_bundled_loaded_and_raw_policy`
- `tests/unit/test_fill_evidence.py::test_lane_order_defaults_do_not_share_mutable_maps`
- `tests/unit/test_policy.py::test_admission_settings_default_and_validate`
- `tests/unit/test_scheduler_fill.py::test_default_decisions_equal_frozen_release217`

The command was `.venv/bin/python -m pytest -p sf_fill_scratch -p no:cacheprovider --basetemp /private/tmp/sf-fill-def-r2/pytest-default-red -q --tb=short @/private/tmp/sf-fill-def-r2/default-test-ids.txt`, with those four lines in the argument file. The original shipped hash was `d63ea22bfd4e769121bef774f95260b1ea7ab3bb6c9b53f85cdd619e1e7d3911`.

Flipped only the Claude order in `subfleet/default_policy.json` and `ADMISSION_DEFAULTS` in `subfleet/policy.py`. `shasum -a 256 subfleet/default_policy.json` returned the draft's `b362dc60a0c27ba6c45d0a2201cbf52e80c4ab5dadeeb915c59837eee4826e29`. Pinned it in `test_shipped_policy_preserves_raw_hash_and_disabled_ramp_defaults` and the four cases of `test_missing_cap_preserves_bytes_and_hash`. Their comments name D-FILL1 Default fix r2.

The same pytest command, with `--basetemp /private/tmp/sf-fill-def-r2/pytest-default-green` and the additional argument file `@/private/tmp/sf-fill-def-r2/hash-test-ids.txt` containing those two hash test selectors, returned 12 passed in 22.91 s. The four changed tests and the two hash pins passed together. Applied the archived hunks by file, with the fill fixture change reserved for step 4.

### Why reset ships

The saved replay from 2026-10-31T23:51:40Z through 2026-11-07T23:51:40Z covers 309 attempts, including 85 on Claude. At spread 2, fill with the 21,600 s expiring tier modeled 15 limited Claude attempts versus 7 under reset. The hourly mean of partly used Claude accounts went from 4.62 to 4.29 and the top account's share of Claude attempt-hours went from 28.53% to 24.93%. Reset modeled 7 limits where the store recorded 3, and moved 81 of 309 attempts to another lane. The replay imputes all 85 Claude five-hour costs as zero and keeps historical ramp closures fixed. The figures were checked against `2026-11-08-fill-first-replay.json`. The model cannot size fill's cost, and its modeled limit count favors the order with production history.

Either of these can support a hub ruling to change Claude's default.

- A replay calibrated to the store. Reset reproduces the recorded Claude limited attempts for the interval, and the replay uses observed five-hour costs. Fill models at most as many limited Claude attempts as reset and fewer accounts partly used at once.
- A measured period with fill selected in policy after D-DESKTOP-ORG1 is installed. Claude limits per recorded attempt are at most the rate from a reset period at similar load, and the desktop app switches accounts fewer times.

### Policy and horizons

The shipped file and `ADMISSION_DEFAULTS` ship `{"claude": "reset", "codex": "reset"}`. Omitted providers in explicit maps use reset. `lane_spread` defaults to the scalar 2, and `expiring_s` defaults to 21600 and applies only under fill. Fill uses its existing comparator, evidence, validation and clock paths, including the store's fill query.

To opt in, set `admission.lane_order` to `{"claude": "fill"}` in the existing state-root `policy.json` and restart the daemon. `Daemon.__init__` loads the file at `subfleet/daemon.py:1025` and computes its hash at line 1026. Startup copies the shipped bytes when the file is absent (lines 1023–1024), so a previous explicit fill setting persists until edited. `_pick` passes the digest to `scheduler.evaluate` (`daemon.py:2833–2834`), which records it in the decision (`scheduler.py:1446–1448`). The stored digest identifies the exact policy file bytes when inspecting a decision with `why` (`policy.py:375–377`, `daemon.py:9207–9208`). The reservation check separately verifies policy object identity (`daemon.py:9351`).

The only production call to `capacity.lane_horizons` is `Daemon._pick` at `daemon.py:2837`, which passes its configured policy and that policy's reading TTL. `capacity.decision_horizon` has no production call site and forwards its optional policy to `lane_horizons` at `capacity.py:415`. Omitting `policy` now uses reset clocks. A caller that omitted it while the operator had enabled fill would miss the fill clocks.

The steps 2 and 3 staging attempt hit the same `index.lock` denial.

### Fill coverage scope

Took the draft's dual-order horizon fixture and passed `policy=` to the horizon functions. Only providers selecting fill contribute fill-window clocks. Each of the three seeded laws has separate reach assertions under reset and fill. The fixed reserve-closure horizon example selects fill in its own policy. The fill evidence fixture selects fill explicitly. The direct `fill_ranking_usage` examples use admission defaults for their numeric settings, so the order selection belongs to the fixture users that evaluate a lane.

The shared Hypothesis policy strategy draws both orders when a law opts in. The four C-6.3 laws in `test_route_check.py` use the draw to cover both comparators without multiplying their example budget. The cross-provider horizon example selects fill in its own test. The flagged laws for caps, admission, steering, waiter selection and pins opt in where they evaluate lanes. Other strategy users, including the unprobed held-waiter tests, keep the shipped default. The frozen evaluator comparisons retain their explicit reset pins.

The draft's per-test selections were taken, narrowed or dropped as follows.

- `tests/fake/conftest.py` drops the `Harness` selection. Each applicable fake test sets its own policy, or its state machine draws the order.
- `test_admission_route_isolation.py` moves the draft's shared `policy()` selection into the flagged evaluate-and-demand-lanes example. Other flagged fake cases set fill in their test-owned policies.
- `test_review_needs_shell.py` moves the shared `routing_cases()` selection into the generated capability law as a two-order draw. The fixed skip-to-later-Codex example selects fill. The frozen baseline keeps its reset map.
- `test_admission_uncapped.py` drops the fixed desktop selection in favor of the both-order desktop law, and changes the band property to draw both orders. The parent-cap and Sep 27 jam examples retain local fill.
- `test_capacity_steer_route.py` drops the fixture-wide fill selection. Two flagged steering properties and the independent oracle draw both orders. The flagged off-walk case selects fill locally.
- `test_claude_usage.py` and `test_cli.py` take the draft's local picks for the removed reserve window and exclusion explanation.
- `test_picker.py` drops the helper-wide selection. Its four flagged cases each select fill locally.
- `test_policy_sandbox_resolution.py` moves the helper-wide selection into the lane-rejection test.
- `test_scheduler_tier_preferences.py` drops the `preference_policy()` selection. The flagged properties draw both orders, two fixed examples select fill in their policy, and the frozen comparison keeps its reset map.
- `test_scoped_stream_capacity.py` takes the draft's `fill_policy()`. All five call sites were flagged.
- `test_unadmittable_pin.py` drops the module-wide `FILL_POLICY` default. The refusal, re-enrolment and notice examples use a helper local to those three tests. The 38-job incident and temporary refusal example use the named both-order laws. The malformed-route example keeps reset. The generated pin laws draw both orders.
- `test_weekly_rank_vs_base.py` drops the helper-wide selection. The Claude strategy draws both orders and renewal examples select fill locally. The exact renewal horizon receives that policy. The draft change to the unflagged Codex stale-label horizon is dropped.

The read-only source audit confirmed the reset selections in `test_scheduler.py`, `test_scheduler_split.py`, `test_scheduler_tier_preferences.py`, `test_scheduler_weekly.py` and `test_review_needs_shell.py`. The archived hunks were read with `git diff 42da87eb5 4066743d9 -- <path>`, originals classified with `git diff 4aed7a3c9 42da87eb5 -- <path>`, and the changes passed in-memory Python compilation and `git diff --check`.

Each untouched fixed example marked as selecting fill exercises an assertion for which the suite had no same-law fill witness.

### Probe comparison

The pristine probe reached fill in 307 failing IDs. The configured probe reached fill in 287 failing IDs. 284 IDs match exactly, 23 left the same-ID failure set, and 3 IDs entered it. The new parametrized horizon properties use the `[fill]` IDs in the second run. Of the baseline IDs, 20 have named fill witnesses, 233 select fill, and 54 cover both orders.

`.venv/bin/python /private/tmp/sf-fill-def-r2/probe.py after 1` returned 328 failed, 2565 passed, 2 skipped in 1085.87s (0:18:05). 41 failures lacked the fill marker. Both runs list their exact commands, restored hashes and complete IDs in the probe JSON.

The list groups parameter variants under their function. The JSON holds each full ID and its witness.

- `tests/fake/test_admission_liveness.py`
  - `test_c6_3_c26_9_admission_places_what_e053b2c_placed_pass_for_pass` — Both orders.
  - `test_c6_3_readings_ageing_out_on_sixty_unrelated_lanes_never_hold_a_job_back` — Reset with fill covered by `tests/unit/test_route_check.py::test_c6_3_lanes_a_decision_never_looks_at_cannot_change_it`.
- `tests/fake/test_admission_route_isolation.py`
  - `test_c11_2_a_dry_run_resolves_the_same_way` — Selects fill.
  - `test_c11_2_a_later_roster_change_cannot_make_an_accepted_pin_ambiguous` — Selects fill.
  - `test_c11_2_a_pin_follows_its_lane_through_a_reenrolment` — Selects fill.
  - `test_c11_2_evaluate_and_demand_lanes_narrow_the_same_way` — Selects fill.
  - `test_c11_2_the_jobs_provider_narrows_a_pin_that_names_two_lanes` `[changes0-claude-a]`, `[changes1-codex-1]`, `[changes2-claude-a]`, `[changes3-codex-1]` — Selects fill.
  - `test_c4_5_a_retry_let_go_is_evaluated_again_when_it_is_next_due` — Selects fill.
  - `test_c4_5_a_retry_that_followed_a_reenrolment_counts_the_account_once` — Selects fill.
  - `test_c4_5_a_retry_whose_credential_is_latched_goes_to_the_next_candidate` — Selects fill.
  - `test_c4_5_a_retry_whose_lane_is_only_full_keeps_its_lane` — Selects fill.
  - `test_c4_5_a_retry_whose_lane_refuses_it_for_more_than_a_slot_goes_to_the_next_candidate` `[closed]`, `[desktop]`, `[excluded]` — Selects fill.
  - `test_c6_12_a_pin_that_names_two_lanes_is_refused_and_the_pass_goes_on` `[later_changes0]`, `[later_changes1]`, `[later_changes2]` — Selects fill.
  - `test_c6_12_a_refused_check_rolls_back_what_the_reservation_wrote` — Selects fill.
  - `test_c6_12_a_transient_retry_on_a_model_id_the_policy_dropped_routes_the_job_itself` — Selects fill.
  - `test_c6_12_a_transient_retry_whose_id_is_retired_to_another_provider_routes_the_job_itself` — Selects fill.
  - `test_c6_12_a_transient_retry_whose_lane_was_disabled_since_routes_the_job_itself` — Selects fill.
  - `test_c6_12_an_authorization_bound_to_a_label_is_refused` — Selects fill.
  - `test_c6_12_an_authorization_the_probe_check_rejects_is_refused_and_the_pass_goes_on` `[1]`, `[2]` — Selects fill.
  - `test_c6_12_an_empty_task_or_tier_is_none` — Selects fill.
  - `test_c6_12_an_evaluation_error_holds_the_job_visibly_and_holds_nobody_else` — Selects fill.
  - `test_c6_12_an_evaluation_made_again_off_the_lock_is_isolated_too` — Selects fill.
  - `test_c6_12_an_evaluation_that_succeeds_resets_the_route_count` — Selects fill.
  - `test_c6_12_any_evaluation_error_of_one_job_is_that_jobs` `[error0]`, `[error1]` — Selects fill.
  - `test_c6_12_the_incident_a_job_pinned_by_an_ambiguous_email_does_not_stop_the_pass` — Selects fill.
  - `test_c6_12_the_incident_through_the_worker_pool_logs_no_failure` — Selects fill.
  - `test_c6_3_a_cap_that_fills_before_the_reservation_is_judged_on_every_lane` — Selects fill.
  - `test_c6_3_a_commit_that_changes_nothing_the_decision_rests_on_does_not_send_it_back` — Selects fill.
  - `test_c6_3_a_commit_that_moves_the_decision_is_decided_again_from_the_lanes_it_changed` — Selects fill.
  - `test_c6_3_a_long_wait_for_the_store_lock_does_not_void_the_decision` — Selects fill.
  - `test_c6_3_the_reservation_takes_the_early_evaluation_when_nothing_was_committed` — Selects fill.
  - `test_c6_9_a_due_retry_is_looked_at_as_its_pair_before_it_is_evaluated` — Selects fill.
  - `test_c6_9_a_pinned_retry_competes_only_for_its_own_pair` — Selects fill.
  - `test_c6_9_a_retry_that_lets_its_pin_go_keeps_its_place_behind_older_jobs` `[a-loaded-runner]`, `[prompt]` — Selects fill.
  - `test_c6_9_a_younger_job_does_not_pass_a_retry_that_let_its_pin_go` `[after-a-loaded-runner]`, `[after-prompt]`, `[before-the-retry-is-due-a-loaded-runner]`, `[before-the-retry-is-due-prompt]`, `[when-it-is-due-a-loaded-runner]`, `[when-it-is-due-prompt]` — Selects fill.
- `tests/fake/test_admission_stateful.py`
  - `TestAdmissionPass::runTest` — Both orders.
  - `TestAdmissionPassProviderCaps::runTest` — Both orders.
  - `test_adm3_cancelled_job_does_not_remain_an_older_waiter` — Reset with fill covered by `tests/fake/test_admission_stateful.py::TestAdmissionPassProviderCaps::runTest`.
  - `test_adm3_started_job_does_not_remain_an_older_waiter` `[1]`, `[2]` — Selects fill.
  - `test_r4_simulated_finish_preserves_provider_outcome` `[limited-limited-claude-sonnet]`, `[model-capacity-transient-claude-sonnet]`, `[ok-ok-claude-sonnet]`, `[transient-transient-claude-sonnet]` — Selects fill.
- `tests/fake/test_policy_sandbox.py`
  - `test_a_launch_starts_codex_at_the_top_and_claude_in_the_callers_place` `[claude]` — Selects fill.
- `tests/fake/test_probe_turn.py`
  - `test_a_check_that_walks_back_to_the_jobs_own_model_is_reserved` — Selects fill.
  - `test_no_test_here_bounds_how_long_the_host_takes` `[rotation-121s-after-the-reading]`, `[walk-back-121s-after-the-reading]` — Selects fill.
  - `test_rotation_never_promotes_at_the_reservation` — Selects fill.
  - `test_rotation_never_takes_a_model_the_chain_promotes_to` — Selects fill.
- `tests/fake/test_provider_active_cap_fake.py`
  - `test_claude_only_cap_is_symmetric` — Selects fill.
  - `test_codex_waiter_does_not_hold_a_claude_lane_only_pin` — Reset with fill covered by `tests/fake/test_admission_stateful.py::TestAdmissionPassProviderCaps::runTest`.
  - `test_existing_caps_preserve_fifo_for_the_selected_provider` `[max_active_attempts-3]`, `[max_active_attempts_per_parent-1]`, `[max_in_flight_per_lane-2]` — Selects fill.
  - `test_mixed_chains_wait_only_for_selected_provider_with_existing_caps` — Both orders.
  - `test_mixed_provider_chain_keeps_fifo_only_on_the_provider_it_uses` `[False]` — Selects fill.
  - `test_mixed_provider_chain_keeps_fifo_only_on_the_provider_it_uses` `[True]` — Reset with fill covered by `tests/fake/test_provider_active_cap_fake.py::test_mixed_chains_wait_only_for_selected_provider_with_existing_caps`.
  - `test_n_plus_one_jobs_hold_and_resume_in_fifo` `[1]`, `[2]`, `[3]` — Selects fill.
  - `test_other_pool_changes_after_evaluation_are_seen_in_transaction` `[False-dispatch]`, `[False-turn]`, `[True-dispatch]`, `[True-turn]` — Selects fill.
  - `test_probe_reservations_do_not_consume_provider_slots` — Selects fill.
  - `test_provider_fifo_respects_lane_only_pins_and_keeps_last_slot` — Reset with fill covered by `tests/fake/test_admission_stateful.py::TestAdmissionPassProviderCaps::runTest`.
  - `test_quarantine_census_change_is_revalidated_before_reservation` `[False-detached]`, `[False-turn]`, `[True-detached]`, `[True-turn]` — Selects fill.
  - `test_quarantine_retains_provider_slot_until_contained_or_released` `[detached-detached-False-empty]`, `[detached-detached-False-live]`, `[detached-detached-False-unknown]`, `[detached-detached-True-empty]`, `[detached-detached-True-live]`, `[detached-detached-True-unknown]`, `[detached-turn-False-empty]`, `[detached-turn-False-live]`, `[detached-turn-False-unknown]`, `[detached-turn-True-empty]`, `[detached-turn-True-live]`, `[detached-turn-True-unknown]`, `[turn-detached-False-empty]`, `[turn-detached-False-live]`, `[turn-detached-False-unknown]`, `[turn-detached-True-empty]`, `[turn-detached-True-live]`, `[turn-detached-True-unknown]`, `[turn-turn-False-empty]`, `[turn-turn-False-live]`, `[turn-turn-False-unknown]`, `[turn-turn-True-empty]`, `[turn-turn-True-live]`, `[turn-turn-True-unknown]` — Selects fill.
  - `test_random_arrivals_and_completions_never_overbook_at_reservation` — Both orders.
  - `test_same_provider_fifo_crosses_models_and_lanes_while_count_is_below_cap` — Reset with fill covered by `tests/fake/test_admission_stateful.py::TestAdmissionPassProviderCaps::runTest`.
  - `test_turn_priority_when_a_provider_slot_frees` — Selects fill.
  - `test_turns_count_and_are_held_by_detached_attempts` `[finalizing-dispatch]`, `[finalizing-turn]`, `[reserved-dispatch]`, `[reserved-turn]`, `[running-dispatch]`, `[running-turn]`, `[starting-dispatch]`, `[starting-turn]` — Selects fill.
  - `test_verified_empty_quarantine_census_wakes_provider_waiter` `[live-detached]`, `[live-turn]`, `[unknown-detached]`, `[unknown-turn]` — Selects fill.
- `tests/fake/test_review_needs_shell.py`
  - `test_hard_review_waits_for_codex_then_runs_with_shell` — Selects fill.
  - `test_needs_shell_only_places_on_capable_launch_or_waits` — Both orders.
  - `test_new_policy_models_route_to_codex` `[luna]`, `[sol61]` — Selects fill.
  - `test_opus_attempt_and_final_notice_line_report_shell` `[read-only-False-False]`, `[workspace-write-None-True]` — Selects fill.
  - `test_requirement_can_skip_opus_for_later_codex_model` — Selects fill.
  - `test_resolved_shell_requirement_survives_policy_change` — Selects fill.
  - `test_resume_keeps_explicit_shell_opt_out` — Selects fill.
- `tests/fake/test_route_view.py`
  - `test_a_route_reads_less_and_decides_the_same` — Both orders.
  - `test_pick_reads_the_route_rows_and_status_live_rows` — Selects fill.
  - `test_the_reservation_recheck_reads_the_same_from_the_route_rows` — Both orders.
- `tests/fake/test_routing_tier_preferences.py`
  - `test_admission_probe_uses_same_tier_fallback_model` — Selects fill.
  - `test_admission_why_and_status_use_same_tier_fallback` `[easy-haiku-sonnet-claude-1]`, `[hard-sol61-opus-claude-1]`, `[standard-sol61-opus-claude-1]` — Selects fill.
  - `test_pins_and_probe_reach_use_first_preferred_provider` — Selects fill.
- `tests/fake/test_unadmittable_pin_admission.py`
  - `test_c11_8_a_closure_within_the_horizon_is_a_wait_not_a_hold` — Selects fill.
  - `test_c11_8_a_job_every_lane_refuses_for_good_holds_nobody_back_either` — Selects fill.
  - `test_c11_8_a_job_with_an_attempt_still_quarantined_is_held_not_failed` — Selects fill.
  - `test_c11_8_a_lane_that_recovers_takes_its_job_at_once_and_ends_the_episode` — Selects fill.
  - `test_c11_8_a_notice_the_caller_was_shown_stays_when_the_job_fails` — Selects fill.
  - `test_c11_8_a_restart_keeps_the_clock_and_sends_no_second_notice` — Selects fill.
  - `test_c11_8_a_state_that_lasts_one_pass_tells_nobody_anything` — Selects fill.
  - `test_c11_8_a_turn_is_held_and_nothing_more` — Selects fill.
  - `test_c11_8_held_told_once_blocking_nobody_then_failed_rc_3` `[credential-latched]`, `[desktop]`, `[disabled]`, `[excluded]`, `[held]`, `[identity-mismatch]`, `[owner-v1]`, `[unknown]` — Selects fill.
  - `test_c11_8_one_notice_per_job_however_often_its_lane_flips` — Selects fill.
  - `test_c11_8_with_a_null_grace_it_waits_and_is_told_so` — Selects fill.
  - `test_c11_8_with_no_grace_it_fails_on_the_pass_that_finds_it_with_one_notice` — Selects fill.
  - `test_c6_9_c11_8_in_the_daemon_no_job_waits_behind_a_job_no_lane_can_admit` — Both orders.
  - `test_c6_9_c11_8_the_incident_a_desktop_pin_holds_back_none_of_the_opus_jobs_after_it` — Reset with fill covered by `tests/fake/test_unadmittable_pin_admission.py::test_c6_9_c11_8_in_the_daemon_no_job_waits_behind_a_job_no_lane_can_admit`.
- `tests/unit/test_admission_uncapped.py`
  - `test_a_band_as_wide_as_the_per_lane_cap_chooses_as_the_cap_did` — Both orders.
  - `test_a_pass_is_deterministic_whatever_order_rows_arrive_in` — Both orders.
  - `test_bands_fill_lanes_evenly_with_no_cap` — Both orders.
  - `test_jobs_are_considered_by_class_then_tier_then_age` — Both orders.
  - `test_raising_a_cap_never_removes_a_candidate` — Both orders.
  - `test_raising_or_removing_a_cap_never_places_fewer_jobs` — Both orders.
  - `test_the_2026_09_27_jam_places_everything_uncapped` — Selects fill.
  - `test_the_desktop_lane_is_out_while_in_use_and_last_otherwise` — Both orders.
  - `test_the_desktop_lane_takes_only_what_no_other_lane_can` — Reset with fill covered by `tests/unit/test_admission_uncapped.py::test_the_desktop_lane_is_out_while_in_use_and_last_otherwise`.
  - `test_the_guard_holds_detached_jobs_at_the_door_and_never_a_turn` — Both orders.
  - `test_uncapped_evaluate_never_says_no_slot_for_want_of_a_count` — Both orders.
  - `test_uncapped_every_admissible_job_is_placed_and_none_waits_for_a_count` — Both orders.
  - `test_with_only_a_parent_cap_a_job_waits_only_behind_its_family` — Selects fill.
- `tests/unit/test_capacity_desktop.py`
  - `test_a_mismatched_lane_is_not_a_candidate` — Reset with fill covered by `tests/unit/test_scheduler_fill.py::test_fill_and_reset_have_identical_candidates_and_refusals[claude-opus]`.
- `tests/unit/test_capacity_steer_route.py`
  - `test_c11_2_a_steer_inside_the_upward_walk_changes_routing` — Reset with fill covered by `tests/unit/test_capacity_steer_route.py::test_c11_2_the_steered_decision_is_the_first_model_of_its_walk_with_a_candidate`.
  - `test_c11_2_a_steer_on_a_model_outside_the_walk_changes_nothing` `[standard-expected1]` — Selects fill.
  - `test_c11_2_a_steered_hard_review_goes_to_the_chains_next_model` — Reset with fill covered by `tests/unit/test_capacity_steer_route.py::test_c11_2_the_steered_decision_is_the_first_model_of_its_walk_with_a_candidate`.
  - `test_c11_2_the_steered_decision_is_the_first_model_of_its_walk_with_a_candidate` — Both orders.
  - `test_c11_2_with_no_lane_for_the_next_model_the_job_waits` — Reset with fill covered by `tests/unit/test_capacity_steer_route.py::test_c9_5_any_chain_and_admissible_lanes_choose_first_unsteered_or_wait`.
  - `test_c6_3_the_reservation_check_of_a_steered_decision_is_a_fresh_evaluation` — Both orders.
  - `test_c9_5_any_chain_and_admissible_lanes_choose_first_unsteered_or_wait` — Both orders.
- `tests/unit/test_claude_usage.py`
  - `test_c11_7_complete_shared_only_snapshot_releases_removed_reserved_window` — Selects fill.
- `tests/unit/test_cli.py`
  - `test_why_renders_exclusion_from_real_policy_decision` — Selects fill.
- `tests/unit/test_decision_horizon.py`
  - `test_a_closure_that_ends_is_a_horizon_even_when_it_closes_a_lane` — Selects fill.
  - `test_every_rank_key_is_constant_before_its_lanes_horizon` — Both orders.
  - `test_the_clock_alone_changes_no_decision_before_its_horizon` — Both orders.
  - `test_the_clock_alone_changes_no_lane_before_its_own_horizon` — Both orders.
- `tests/unit/test_fill_evidence.py`
  - `test_auxiliary_provider_window_restored_when_reset_override_ends` — Selects fill.
  - `test_cached_fill_verdict_matches_full_evaluation_before_expiring_and_reset_clocks` `[1-0]`, `[14400-1]`, `[36000-1]` — Selects fill.
  - `test_equal_observation_ties_are_conservative_and_independent_of_reading_id` — Selects fill.
  - `test_latest_numeric_observation_with_no_open_window_does_not_revive_an_older_one` — Selects fill.
  - `test_store_view_preserves_latest_observed_numeric_provider_reading_behind_a_mask` `[mask0]`, `[mask1]`, `[mask2]`, `[mask3]` — Selects fill.
- `tests/unit/test_fill_routing.py`
  - `test_fill_cached_route_matches_evaluate_at_old_window_boundaries` `[expiring]`, `[reset]` — Selects fill.
  - `test_fill_expiring_horizon_accepts_arbitrarily_large_whole_seconds` — Selects fill.
  - `test_fill_pick_and_why_show_the_same_fields_and_keep_freshness` — Selects fill.
  - `test_fill_unknown_model_pick_binds_all_applicable_scopes` — Selects fill.
  - `test_per_provider_spread_maps_apply_to_each_ranking_order` — Selects fill.
- `tests/unit/test_lease_waiter_selection.py`
  - `test_a_job_that_passes_a_lease_waiter_keeps_the_last_slot_for_it` — Both orders.
  - `test_a_lease_waiter_is_held_for_no_count_once_its_lease_is_let_go` — Both orders.
  - `test_no_job_is_held_behind_a_job_waiting_for_a_lease` — Both orders.
  - `test_with_no_lease_wait_the_pass_is_the_rule_before` — Both orders.
- `tests/unit/test_picker.py`
  - `test_case_insensitive_claude_email_aliases_are_ambiguous` — Selects fill.
  - `test_claude_email_exact_model_reserve_and_exclusion` — Selects fill.
  - `test_matched_stream_windows_can_measure_real_opus_reserve_slack` — Selects fill.
  - `test_reserve_slack_requiring_probe_is_not_a_raw_pick` — Selects fill.
- `tests/unit/test_policy_sandbox_resolution.py`
  - `test_lane_rejection_resolves_policy_permissions` `[context0-read-only-None]`, `[context0-read-only-policy]`, `[context1-workspace-write-None]`, `[context1-workspace-write-policy]`, `[context2-read-only-None]`, `[context2-read-only-policy]`, `[context3-read-only-None]`, `[context3-read-only-policy]`, `[context4-read-only-None]`, `[context4-read-only-policy]`, `[context5-read-only-None]`, `[context5-read-only-policy]`, `[context6-workspace-write-None]`, `[context6-workspace-write-policy]` — Selects fill.
- `tests/unit/test_route_check.py`
  - `test_c6_3_lanes_a_decision_never_looks_at_cannot_change_it` — Both orders.
  - `test_c6_3_readings_ageing_out_on_another_providers_lanes_are_never_looked_at` — Selects fill.
  - `test_c6_3_still_stands_exactly_when_commits_land_on_the_lanes_it_walked` — Both orders.
  - `test_c6_3_still_stands_exactly_when_evaluate_now_chooses_the_same` — Both orders.
  - `test_c6_3_with_nothing_committed_the_early_decision_stands` — Both orders.
- `tests/unit/test_scheduler_fill.py`
  - `test_fill_and_reset_have_identical_candidates_and_refusals` `[claude-opus]`, `[codex-astra]` — Both orders.
  - `test_fill_complete_chain_has_identical_candidates_and_rejection_predicates` — Both orders.
  - `test_fill_desktop_last_and_turn_affinity_leads_its_class` `[claude-opus]`, `[codex-astra]` — Selects fill.
  - `test_fill_expiring_precedes_nonexpiring_and_earlier_expiring_reset_wins` — Selects fill.
  - `test_fill_expiring_reset_order_keeps_fractional_seconds` — Selects fill.
  - `test_fill_keeps_old_open_provider_reading_and_ignores_expired_one` `[121]`, `[500000]`, `[86400]` — Selects fill.
  - `test_fill_keeps_the_same_probe_predicate_for_each_pinned_lane` `[claude-opus]`, `[codex-astra]` — Both orders.
  - `test_fill_keys_are_unique_total_and_order_independent` `[claude-opus]`, `[codex-astra]` — Selects fill.
  - `test_fill_more_weekly_use_never_ranks_after_less_in_equal_earlier_tiers` — Selects fill.
  - `test_fill_null_expiring_disables_the_tier` — Selects fill.
  - `test_fill_only_latest_applicable_open_windows_contribute_at_any_age` — Selects fill.
  - `test_fill_packs_a_prefix_of_starting_use_when_expiry_and_bands_are_off` — Selects fill.
  - `test_fill_reserve_complements_are_clear_at_equality_and_low_one_float_above` `[five_hour-0.02-0.1-claude-opus]`, `[five_hour-0.02-0.1-codex-astra]`, `[five_hour-0.03-0.4-claude-opus]`, `[five_hour-0.03-0.4-codex-astra]`, `[five_hour-0.25-0.125-claude-opus]`, `[five_hour-0.25-0.125-codex-astra]`, `[seven_day-0.02-0.1-claude-opus]`, `[seven_day-0.02-0.1-codex-astra]`, `[seven_day-0.03-0.4-claude-opus]`, `[seven_day-0.03-0.4-codex-astra]`, `[seven_day-0.25-0.125-claude-opus]`, `[seven_day-0.25-0.125-codex-astra]` — Selects fill.
  - `test_fill_retains_band_then_claude_stranding_before_usage` — Selects fill.
- `tests/unit/test_scheduler_tier_preferences.py`
  - `test_closed_first_model_uses_next_in_same_tier_before_higher_tier` — Both orders.
  - `test_preference_examples_and_pins` — Selects fill.
  - `test_reservation_recheck_matches_fresh_routing_for_mixed_chains` — Both orders.
  - `test_standing_refusals_check_every_preferred_model` — Selects fill.
- `tests/unit/test_scoped_stream_capacity.py`
  - `test_guessed_operator_expired_or_released_closures_do_not_release_reserve` `[change0]`, `[change1]`, `[change2]`, `[change3]` — Selects fill.
  - `test_incomplete_or_unrelated_stream_windows_do_not_release_reserve` `[attempt]`, `[missing]`, `[time]` — Selects fill.
  - `test_reported_fable_exhaustion_requires_opus_probe_even_for_read_only` — Selects fill.
  - `test_same_stream_measures_spare_opus_without_spending_fable` — Selects fill.
  - `test_shared_exhaustion_still_blocks_opus_with_fable_closed` — Selects fill.
- `tests/unit/test_unadmittable_now.py`
  - `test_c6_20_a_job_refused_for_good_is_refused_now` — Both orders.
  - `test_c6_20_a_job_refused_now_is_placed_nowhere_now` — Both orders.
  - `test_c6_20_refused_now_is_exactly_the_oracle_and_the_memo_changes_nothing` — Both orders.
- `tests/unit/test_unadmittable_pin.py`
  - `test_c11_8_a_pin_a_reenrolment_moved_follows_it` — Selects fill.
  - `test_c11_8_a_pin_called_unadmittable_is_placed_nowhere_even_in_the_best_capacity` — Both orders.
  - `test_c11_8_a_pin_not_called_unadmittable_is_placed_on_its_lane_in_the_best_capacity` — Both orders.
  - `test_c11_8_a_shorter_horizon_never_frees_a_pin_a_longer_one_holds` — Both orders.
  - `test_c11_8_each_standing_refusal_is_named` — Selects fill.
  - `test_c11_8_refused_for_good_agrees_with_the_oracle_for_every_job` — Both orders.
  - `test_c11_8_the_notice_and_the_failure_say_what_and_what_to_do` — Selects fill.
  - `test_c11_8_unadmittable_is_exactly_the_oracle_for_every_job_and_the_memo_changes_nothing` — Both orders.
  - `test_c11_8_what_a_wait_ends_is_not_standing` — Reset with fill covered by `tests/unit/test_unadmittable_pin.py::test_c11_8_unadmittable_is_exactly_the_oracle_for_every_job_and_the_memo_changes_nothing`.
  - `test_c6_9_c11_8_no_job_waits_behind_a_job_no_lane_can_admit` — Both orders.
  - `test_c6_9_c11_8_the_incident_38_jobs_are_not_held_behind_the_desktop_pin` — Reset with fill covered by `tests/unit/test_unadmittable_pin.py::test_c6_9_c11_8_no_job_waits_behind_a_job_no_lane_can_admit`.
- `tests/unit/test_weekly_rank_vs_base.py`
  - `test_a_bucket_the_provider_stopped_reporting_does_not_unmeasure_a_freshly_read_lane` — Reset with fill covered by `tests/unit/test_weekly_rank_vs_base.py::test_no_lane_newly_needs_a_pre_launch_probe[claude-True]`.
  - `test_equal_weekly_headroom_binds_the_earliest_reset[False-claude-opus]` — Reset with fill covered by `tests/unit/test_scheduler_fill.py::test_fill_equal_weekly_use_binds_the_earliest_reset[account]`.
  - `test_equal_weekly_headroom_binds_the_earliest_reset[True-claude-opus]` — Reset with fill covered by `tests/unit/test_scheduler_fill.py::test_fill_equal_weekly_use_binds_the_earliest_reset[model-scope]`.
  - `test_every_lane_is_judged_as_the_base_judged_it` `[64b4175f-claude]` — Both orders.
  - `test_no_lane_newly_needs_a_pre_launch_probe` `[claude-False]`, `[claude-True]` — Both orders.
  - `test_pick_excludes_no_lane_the_base_recommended` `[claude-False]`, `[claude-True]` — Both orders.
  - `test_pick_keeps_a_freshly_read_lane_beside_a_bucket_the_provider_stopped_reporting` — Reset with fill covered by `tests/unit/test_weekly_rank_vs_base.py::test_pick_excludes_no_lane_the_base_recommended[claude-True]`.
  - `test_recent_renewal_uncertainty_ends_at_its_ttl_with_a_stable_horizon` — Selects fill.
  - `test_stopped_windows_do_not_demote_a_lane_forever` `[account-claude-fable]`, `[model-claude-fable]` — Reset with fill covered by `tests/unit/test_scheduler_fill.py::test_fill_only_latest_applicable_open_windows_contribute_at_any_age`.
  - `test_why_distinguishes_a_recent_renewal_from_admission_freshness` `[0]`, `[119]`, `[30]` — Selects fill.

The second probe had the same 41 importer path failures. The baseline's CLI timing failure passed in this run. All 307 baseline fill-reach IDs have a verified disposition. Guarded cleanup removed completed `pytest-probe-after-1/popen-gw0/admission0` through `admission176`, and then the completed basetemp.

Step 4 staging hit the same `index.lock` denial.

### Mutants

Took the draft's three default mutant changes and its JSON parsing for the shipped-file target. Named the exact parameter IDs for the TTL, expired-window and desktop mutants. The seven unique selectors in `mutant-test-ids.txt` were checked with `.venv/bin/python -m pytest -p sf_fill_scratch -p no:cacheprovider --basetemp /private/tmp/sf-fill-def-r2/pytest-mutant-collect --collect-only -q @/private/tmp/sf-fill-def-r2/mutant-test-ids.txt`. Pytest collected all seven in 1.22 s.

`.venv/bin/python /private/tmp/sf-fill-def-r2/run-mutants.py --basetemp /private/tmp/sf-fill-def-r2/pytest-mutants-r2 --output docs/reports/2026-11-08-fill-first-mutants-default-r2.json` ran the tool with disk and time checks before each child. All eight children reported one assertion failure and zero passes. The tool restored the bytes of every target and attested their SHA-256s. [Mutation evidence](2026-11-08-fill-first-mutants-default-r2.json)

| Mutant | Test in `tests/unit/test_scheduler_fill.py` |
| --- | --- |
| `weekly-ascending` | `test_fill_more_weekly_use_never_ranks_after_less_in_equal_earlier_tiers` |
| `ttl-only` | `test_fill_keeps_old_open_provider_reading_and_ignores_expired_one[121]` |
| `expired-counts` | `test_fill_keeps_old_open_provider_reading_and_ignores_expired_one[121]` |
| `expiring-dropped` | `test_fill_expiring_precedes_nonexpiring_and_earlier_expiring_reset_wins` |
| `desktop-dropped` | `test_fill_desktop_last_and_turn_affinity_leads_its_class[claude-opus]` |
| `default-codex-fill` | `test_default_decisions_equal_frozen_release217[codex-astra-omitted]` |
| `default-claude-fill` | `test_default_decisions_equal_frozen_release217[claude-opus-omitted]` |
| `shipped-default-claude-fill` | `test_default_decisions_equal_frozen_release217[claude-opus-shipped]` |

Step 5 staging hit the same `index.lock` denial.

### Contract

C-11.11 states the reset default for both providers, the production-history decision, and both ways the hub can approve a future change. The existing D-FILL1 change-log entry for C-11.11 and C-11.3 names this section and describes fill as a setting. The C-6.3 timing entry still describes the fill path. C-11.3 remains the reset order, C-11.7 sends reserve survivors to the configured comparator, and C-23.37 places stranded lanes first within either order.

### Ordered regression runs

The pytest prefix was `.venv/bin/python -m pytest -p sf_fill_scratch -p no:cacheprovider`. In the commands below, `$scratch` is `/private/tmp/sf-fill-def-r2`. Argument files are described by their selection in the table. Each run checked free disk and used a fresh basetemp.

| Group | Pytest arguments after the prefix | Selection | Result |
| --- | --- | --- | --- |
| g1-unit | `--basetemp $scratch/pytest-g1-unit -q -n 4 -ra --tb=short @$scratch/g1-unit.txt` | 20 changed unit test files | 958 passed in 439.79s (0:07:19) |
| g1-fake | `--basetemp $scratch/pytest-g1-fake -q -n 4 -ra --tb=short @$scratch/g1-fake.txt` | 10 changed fake-daemon test files | 348 passed in 70.12s (0:01:10) |
| g2-fill | `--basetemp $scratch/pytest-g2-fill -q -n 4 -ra --tb=short @$scratch/g2-fill.txt` | Five fill test files | 125 passed in 10.44s |
| g3-clocks | `--basetemp $scratch/pytest-g3-clocks -q -n 4 -ra --tb=short @$scratch/g3-clocks.txt` | Decision horizon, route check, store contention queries | 246 passed in 63.95s (0:01:03) |
| g4-rest | `--basetemp $scratch/pytest-g4-rest -q -n 4 -ra --tb=short @$scratch/g4-rest.txt` | 34 remaining regression-list files other than importer | 1199 passed in 68.74s (0:01:08) |
| g4-importer | `--basetemp /private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261125-222605-fill-def-r2-a1-11i9a6xq/sf-fill-def-r2/pytest-g4-importer -q -n 4 -ra --tb=short @$scratch/g4-importer.txt` | Importer under non-/tmp Darwin session scratch | 149 passed, 2 skipped in 8.38s |
| g5-contract | `--basetemp $scratch/pytest-g5-contract -q -n 4 -ra --tb=short @$scratch/g5-contract.txt` | State, module names, invariants, handler refusals and protocol registry | 441 passed in 26.73s |
| g5-invariants-final | `--basetemp $scratch/pytest-g5-invariants-final -q -n 4 -ra --tb=short tests/unit/test_invariants_index.py` | Contract index after final evidence-bar wording | 25 passed in 2.24s |

The seven ordered regression runs completed with zero failures. The eighth run checked the final C-11.11 wording. The two importer skips at lines 1693 and 2642 require macOS process identity, which the sandbox denied. All requested files ran within the time budget. CI owns the full suite.

The group 1 argument files came from `git diff --name-only 42da87eb5 -- tests`, filtered to changed test modules and split by unit and fake directories. Group 4 used the remainder of the 66-file list after removing the files covered by groups 1–3. Importer ran separately at `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261125-222605-fill-def-r2-a1-11i9a6xq/sf-fill-def-r2` because its synthetic conversations must be outside `/tmp`. That run cleared all 41 path failures from the probes. The CLI timing failure also cleared in ordinary regression.

### Workspace, timing and cleanup

The requested Darwin scratch and uv setup were denied as recorded above. The cached-wheel venv, the scratch cleanup plugin, the alternate importer basetemp and the setup retries were the operational deviations. Two shell wrappers initially stopped before pytest because they used zsh's special `path` and `status` names. Standard argument names fixed both. The first two interrupted probe launches used the same basetemp basename. The finished probes and ordinary test runs used separate basetemps.

The largest observed scratch size was 172 MB before the footprint restart. Guarded cleanup removed `pytest-g4-rest/popen-gw0/admission0` through `admission176` (177 completed examples) during group 4. The run removed created `pytest-probe-before`, `pytest-probe-before-3`, `pytest-probe-before-4`, `pytest-probe-after-1`, `pytest-default-red`, `pytest-default-green`, and `pytest-mutants-r2` under `/private/tmp/sf-fill-def-r2`. The mutant collection created no basetemp. The regression basetemps in the table were deleted after recording their results. The root cleanup removed `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261125-222605-fill-def-r2-a1-11i9a6xq/sf-fill-def-r2` and `/private/tmp/sf-fill-def-r2`, including the scratch-created test homes, logs and drivers, with `shutil.rmtree` guarded on the `sf-fill-def-r2` path component. The shared uv cache was left in place.

`git diff --check`, the shipped policy checksum, source compilation, and the probe and mutant artifact checks passed. The checks compared the historical report sections and the original mutant and replay JSON to their base bytes. The only historical report addition is the banner under its title. All test state came from isolated fixtures. Replay evidence came from the saved report and JSON. The independent review, #318 merge and release merge remain with the hub, along with the acceptance-contract conflict resolution and PR copy.

Step 6 staging again hit the worktree's `index.lock` permission denial. No step produced a commit. Subfleet can collect the changes and the two new evidence files from the assigned worktree.

### Proposed PR body

**Proposed title** `Add opt-in fill lane ordering for Claude (D-FILL1, C-11.11)`

Draft. This PR adds `fill` as an admission lane-order setting (D-FILL1). Claude and Codex ship with `reset`.

#### What changes

- `admission.lane_order` chooses each provider's ranking after a shared prefix of desktop, turn affinity, load band and stranded capacity. The shipped map is `{"claude": "reset", "codex": "reset"}`. To opt in, set `admission.lane_order` to `{"claude": "fill"}` in `policy.json` and restart the daemon.
- Fill ranks lanes that still hold their weekly reserve ahead of lanes that have used into it. It applies the same rule to the five-hour reserve, then ranks lanes whose binding weekly reset is within `admission.expiring_s` (21,600 s by default). It uses the earlier reset among expiring lanes, then the highest weekly usage, fewest in-flight jobs and lane ID. Reset follows C-11.3.
- `admission.lane_spread` accepts a provider map and defaults to the scalar 2.
- `why`, `pick --all` and decision reasons show the fill evidence: `lane_order`, `fill_weekly_used`, `fill_weekly_reset`, `fill_five_hour_used` and `fill_expiring`.
- The contract gains C-11.11.

#### Open before merge

- **Default settled.** The offline replay modeled 15 limited Claude attempts under fill at spread 2 and 7 under reset, where the store recorded 3. Changing Claude's default requires a hub ruling. A calibrated replay must reproduce those recorded reset limits for its interval and use observed five-hour costs. Under fill it must model at most as many limits as reset and fewer accounts partly used at once. A measured period with fill selected in policy after D-DESKTOP-ORG1 is installed qualifies if its recorded Claude limit rate per attempt is at most the reset rate at similar load and the desktop app switches accounts fewer times.
- **Merge order.** #318 (D-DESKTOP-ORG1) merges first to protect the account the desktop app uses when an operator selects fill. The release merge that follows puts `desktop_possible` into the shared comparator prefix.
- **Independent review.** Open.

#### Tests

- The default tests first gave 5 expected failures and 2 passes. After the policy flip, all 12 targeted default and hash checks passed.
- The two 68-file probes found 307 and 287 fill-reach failures. The report accounts for every baseline ID.
- The changed unit and fake-daemon files passed 958 and 348 tests. The five fill files passed 125. The horizon, route-check and store contention group passed 246.
- The remaining regression files passed 1,199 tests. Importer passed 149 with 2 process-inspection skips. The final contract group passed 441, and the invariants rerun passed 25.
- All eight mutants failed their named test by assertion. CI runs the whole suite.

Report `docs/reports/2026-11-08-fill-first.md`, “Default fix r2”.

## Review fix r1

Started at 2026-11-26 10:38:24 UTC on `7f344288ac32fba20f10154379493d6fc339abe9`. The initial status showed no tracked changes or untracked files.

### Release/217 and Git

`git merge-base --is-ancestor 53a7bac93d01afd9165588bcaf789dae75540757 HEAD` returned 0. `gh api repos/MaxGhenis/subfleet-v2/branches/release/217 --jq .commit.sha` returned `a4d21501e987db98150776ba3d065def462645ac`, and `git cat-file -e 'a4d21501e987db98150776ba3d065def462645ac^{commit}'` returned 0. `git merge --no-ff --no-commit a4d21501e987db98150776ba3d065def462645ac` failed before the merge because the sandbox denied `ORIG_HEAD.lock`. The status and unmerged-path checks were empty. I continued from the assigned head. The invariant index belongs to the contract run below.

Git also denied `index.lock` for each named-file staging attempt. I tested the test-only worktree before editing production code, then kept the resulting changes and evidence in the assigned worktree for Subfleet salvage.

### Findings

| Finding | Status and evidence |
| --- | --- |
| P2 1. Fill query under reset | Fixed. `policy.fill_on` checks the provider of each configured model. Daemon route and operator snapshots and `Timers.snapshot` skip fill evidence under the shipped reset policy. Models supplied without a provider by raw timer policies contribute no fill order. `capacity.store_rows` keeps its default and accepts `fill=False`. `test_snapshots_query_fill_rows_only_when_requested` failed before the fix on one unexpected query and passed after it. Its added providerless case exposed a `KeyError` in the first gate and passed with the final guard. The 300-example `test_reset_decisions_and_horizons_ignore_fill_rows` passed before and after. |
| P2 2. In-flight tie | Fixed. `test_fill_fewer_in_flight_ranks_first_at_equal_use` passed before and after and killed `in-flight-dropped`. At equal weekly use, the lane with zero in-flight jobs ranks ahead of the lane with one. |
| P2 3. Binding reset tie | The r1 test put the +1 h reset on the account row and the +3 d reset on the model row at equal use. It passed before and after and killed `binding-latest-reset`. Review fix r2 adds the +1 h model-scope reset against the +3 d account reset, so reset order and scope order disagree. In both cases the +1 h reset binds and `claude-1` ranks first in the expiring tier. |
| P2 4. Future-observation clock | Fixed. `test_fill_horizon_notes_a_future_row_the_admission_view_hides` passed before and after and killed `horizon-fill-future-dropped`. The first of two future observations moves the choice at +10 s and sets the horizon at +10 s. |
| P2 5. Nonfirst model scope | Fixed. `test_unknown_model_pick_binds_a_scope_other_than_the_first_model` passed before and after and killed `picker-fill-first-model-only`. Opus scope contributes 0.9 weekly use to the unknown-model pick. |
| P2 6. Reserve order | Text corrected. The proposed body now puts lanes that still hold their weekly reserve first, then applies the same rule to the five-hour reserve. `test_fill_reserve_complements_are_clear_at_equality_and_low_one_float_above` pins the pre-existing order at equality and one float above. |
| P3 1. Milestone row 3 | Text corrected. C-11.11 states that a lane without a fresh reading can rank first on open-window use while keeping its C-11.4 probe predicate. Row 3 scopes measured-first and the C-11.3 reserve/reset clauses to reset. `test_fill_pick_and_why_show_the_same_fields_and_keep_freshness` exercises the existing behavior. |
| P3 2. Stale report passages | Text corrected. The report names the shipped reset map, 42 scheduler cases and 4,500 configured Hypothesis examples, the current symbols and source locations, and the historical mutant counts. Source locations were checked with `sed -n`. |
| P3 3. Oversized int | Text corrected. Only a Python-built view can hold an integer outside float range. The fill ranking and horizon readers raise `OverflowError`. `test_c_int_too_large_for_a_float_raises_in_the_reader` passed before and after. |
| P3 4. Codex fill route law | Fixed. The new 1,500-example `test_c6_3_codex_fill_still_stands_exactly_when_evaluate_now_chooses_the_same` passed before and after and killed `horizon-fill-claude-only`. The shared policy strategy and existing route laws keep their original draws. |

### Snapshot measurement

The fixture used `random.Random(11)`, 50,000 readings, 20 lanes, two scopes, two windows and the review's mixed labels. SQLite reported 3.53.1. Each case used one warm-up and 21 timed samples. The route rows column counts only fill readings. The post-skip route cases rotated through the same 21 sample rounds. The pre-fix driver executed its warm-up and logged the 21-sample summary. The post-skip driver also logged the warm-up duration.

| Stage and case | Rows | Median ms | Min ms | Max ms |
| --- | ---: | ---: | ---: | ---: |
| Before, latest query | 80 | 657.26 | 100.18 | 1,209.89 |
| Before, fill query | 80 | 4,352.66 | 1,908.54 | 11,359.24 |
| Before, shipped reset route | 80 | 3,490.80 | 838.64 | 11,253.48 |
| Before, Claude fill route | 80 | 1,213.86 | 595.00 | 3,266.22 |
| After skip, latest query | 80 | 304.68 | 77.61 | 636.57 |
| After skip, fill query | 80 | 1,684.36 | 606.66 | 3,398.47 |
| After skip, shipped reset route | 0 | 312.27 | 98.31 | 702.39 |
| After skip, Claude fill route | 80 | 2,024.15 | 660.12 | 3,996.44 |
| After skip, shipped reset route with `fill_on` forced true | 80 | 1,551.84 | 607.57 | 3,383.77 |

Before `uptime` reported 11:48 local and loads 104.94, 86.88 and 64.61, with 79 GB free. After `uptime` reported 12:04 local and loads 15.16, 42.53 and 57.52, with 88 GB free. The same-run reset control attributes a 1,239.57 ms median difference to the query gate under that load. Host load shifted between snapshots, limiting comparisons across runs.

The skip preserved both plans. `EXPLAIN QUERY PLAN` emitted the following rows before and after it.

```text
latest_reading_candidates
1 0 MERGE (UNION ALL)
3 1 LEFT
5 3 CO-ROUTINE top
11 5 SCAN readings USING COVERING INDEX readings_latest
52 3 SCAN top
55 3 SEARCH r USING INDEX readings_latest (lane_id=? AND scope=? AND window=? AND observed_at=?)
83 3 USE TEMP B-TREE FOR ORDER BY
101 1 RIGHT
104 101 SCAN readings
123 101 USE TEMP B-TREE FOR ORDER BY

fill_reading_candidates
1 0 MERGE (UNION ALL)
3 1 LEFT
5 3 CO-ROUTINE top
12 5 SCAN readings USING INDEX readings_latest
68 3 SCAN top
71 3 SEARCH readings USING INDEX readings_latest (lane_id=? AND scope=? AND window=? AND observed_at=?)
111 3 USE TEMP B-TREE FOR ORDER BY
129 1 RIGHT
132 129 SCAN readings
165 129 USE TEMP B-TREE FOR ORDER BY
```

The fill query still scanned the `top` CTE with a non-covering index after the skip. The rewrite scans distinct keys with the existing covering index, seeks the newest qualifying canonical timestamp per key, and joins every eligible tie. The fallback arm still keeps every future canonical row and every noncanonical row that passes the same provider and finite-use filter.

| Interleaved query trial | Rows | Median ms | Min ms | Max ms |
| --- | ---: | ---: | ---: | ---: |
| Original fill SQL | 80 | 2,017.35 | 968.90 | 3,901.80 |
| Distinct keys and indexed seek | 80 | 313.17 | 72.07 | 594.31 |

The candidate returned the same 80 rows in the same order and reduced the median by 84.48%. The median paired reduction was 87.73%. The 11:08:15 UTC trial took 80.09 s. `uptime` reported 12:08 local and loads 50.27, 44.56 and 55.07. Free disk was 84 GB. The production SQL matches the timed SQL. The store equivalence checks passed in the fill and route groups. I kept the rewrite. It uses the existing schema and `readings_latest` index.

```text
fill_reading_candidates with the rewrite
1 0 MERGE (UNION ALL)
3 1 LEFT
5 3 CO-ROUTINE keys
8 5 SCAN readings USING COVERING INDEX readings_latest
23 3 SCAN k
26 3 SEARCH r USING INDEX readings_latest (lane_id=? AND scope=? AND window=? AND observed_at=?)
34 3 CORRELATED SCALAR SUBQUERY 2
40 34 SEARCH readings USING INDEX readings_latest (lane_id=? AND scope=? AND window=? AND observed_at<?)
104 3 USE TEMP B-TREE FOR ORDER BY
122 1 RIGHT
125 122 SCAN readings
158 122 USE TEMP B-TREE FOR ORDER BY
```

### Mutants

The tool ran all thirteen initially in 1,512.41 s and on the final source in 55.19 s. Every child recorded one assertion failure, zero passes and zero skips. The tool uses full node IDs for witnesses outside the scheduler test module and verified that it restored its source files after both runs.

| Mutant | Assertion witness | Initial seconds, UTC, free, load | Final seconds, UTC, free, load |
| --- | --- | --- | --- |
| `weekly-ascending` | `test_fill_more_weekly_use_never_ranks_after_less_in_equal_earlier_tiers` | ≤120 s, 11:24:39, 73G, 119.87 97.84 75.77 | 1.15 s, 12:25:21, 71G, 19.72 23.70 34.52 |
| `ttl-only` | `test_fill_keeps_old_open_provider_reading_and_ignores_expired_one[121]` | ≤87 s, 11:26:39, 73G, 125.82 108.10 82.81 | 0.38 s, 12:25:23, 71G, 19.02 23.49 34.38 |
| `expired-counts` | `test_fill_keeps_old_open_provider_reading_and_ignores_expired_one[121]` | ≤68 s, 11:28:06, 81G, 87.83 102.54 83.24 | 0.47 s, 12:25:24, 71G, 19.02 23.49 34.38 |
| `expiring-dropped` | `test_fill_expiring_precedes_nonexpiring_and_earlier_expiring_reset_wins` | ≤143 s, 11:29:14, 80G, 37.37 84.20 77.83 | 7.13 s, 12:25:25, 71G, 19.02 23.49 34.38 |
| `desktop-dropped` | `test_fill_desktop_last_and_turn_affinity_leads_its_class[claude-opus]` | 274.26 s, 11:31:37, 79G, 16.82 57.36 67.87 | 8.55 s, 12:25:33, 71G, 17.79 23.08 34.11 |
| `default-codex-fill` | `test_default_decisions_equal_frozen_release217[codex-astra-omitted]` | 67.10 s, 11:36:39, 78G, 86.74 79.71 75.09 | 2.94 s, 12:25:42, 71G, 17.33 22.90 33.98 |
| `default-claude-fill` | `test_default_decisions_equal_frozen_release217[claude-opus-omitted]` | 81.37 s, 11:38:01, 77G, 80.32 79.91 75.65 | 2.77 s, 12:25:45, 71G, 16.82 22.70 33.84 |
| `shipped-default-claude-fill` | `test_default_decisions_equal_frozen_release217[claude-opus-shipped]` | 70.49 s, 11:39:42, 77G, 46.57 68.74 71.84 | 2.89 s, 12:25:49, 71G, 16.27 22.49 33.70 |
| `in-flight-dropped` | `test_fill_fewer_in_flight_ranks_first_at_equal_use` | 11.38 s, 11:41:16, 77G, 73.22 70.97 72.33 | 0.36 s, 12:25:52, 71G, 15.45 22.21 33.54 |
| `binding-latest-reset` | `test_fill_equal_weekly_use_binds_the_earliest_reset` | 6.58 s, 11:41:35, 77G, 70.87 70.61 72.17 | 0.32 s, 12:25:53, 71G, 15.45 22.21 33.54 |
| `horizon-fill-future-dropped` | `test_fill_routing.py::test_fill_horizon_notes_a_future_row_the_admission_view_hides` | 8.74 s, 11:41:50, 77G, 70.58 70.56 72.12 | 0.32 s, 12:25:54, 71G, 15.45 22.21 33.54 |
| `picker-fill-first-model-only` | `test_fill_routing.py::test_unknown_model_pick_binds_a_scope_other_than_the_first_model` | 6.18 s, 11:42:05, 77G, 66.20 69.60 71.75 | 0.24 s, 12:25:54, 71G, 15.45 22.21 33.54 |
| `horizon-fill-claude-only` | `test_route_check.py::test_c6_3_codex_fill_still_stands_exactly_when_evaluate_now_chooses_the_same` | 397.90 s, 11:42:20, 77G, 62.48 68.61 71.36 | 20.37 s, 12:25:55, 71G, 15.45 22.21 33.54 |

The first four initial child times are upper bounds measured between adjacent preflights. The first scratch logger used a timing attribute removed in pytest 9.1.1. The runner recorded each named assertion failure and exit 1, and the final run logged every child.

### Ordered test runs

Ordinary pytest runs used `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -p no:cacheprovider -n 4`, a fresh `--basetemp $SF/pytest-<label>`, and `--tb=short`. The three new-test batches and the initial fill module run used `-v`. The other runs used `-q`. Module runs used `-ra`.

| Run | Pass / fail / skip / error | Pytest seconds | Wall seconds | UTC start | Free | Load averages |
| --- | ---: | ---: | ---: | --- | ---: | --- |
| `new-before` | 7 / 1 / 0 / 0 | 280.19 | 300.68 | 10:58:07 | 72G | 101.43 85.22 71.88 |
| `new-after` | 8 / 0 / 0 / 0 | 715.07 | 732.75 | 11:11:00 | 84G | 20.17 35.51 49.68 |
| `fill-files` | 132 / 0 / 0 / 0 | 245.40 | 251.85 | 11:52:27 | 77G | 66.79 54.19 60.04 |
| `route-files` | 247 / 0 / 0 / 0 | 868.69 | 873.60 | 11:56:47 | 79G | 30.58 50.59 57.62 |
| `remaining-1` | 347 / 1 / 0 / 0 | 247.07 | 248.52 | 12:11:22 | 72G | 27.61 45.44 52.14 |
| `remaining-2` | 708 / 0 / 0 / 0 | 137.13 | 138.98 | 12:15:33 | 70G | 27.48 34.77 45.58 |
| `remaining-3` | 251 / 0 / 0 / 0 | 168.56 | 169.80 | 12:17:53 | 71G | 27.35 33.45 43.50 |
| `contract` | 437 / 0 / 0 / 0 | 25.62 | 26.18 | 12:20:43 | 70G | 23.90 30.24 40.38 |
| `no-models` | 91 / 0 / 0 / 0 | 2.96 | 3.56 | 12:21:09 | 70G | 19.29 28.67 39.52 |
| `adjacent-fake` | 35 / 0 / 0 / 0 | 46.73 | 47.26 | 12:21:13 | 71G | 18.14 28.28 39.31 |
| `adjacent-unit` | 265 / 157 / 0 / 0 | 84.63 | 85.54 | 12:22:01 | 71G | 20.27 27.49 38.46 |
| `partial-model-before` | 0 / 1 / 0 / 0 | 1.83 | 2.50 | 12:23:41 | 71G | 16.68 24.58 36.13 |
| `new-after-final` | 8 / 0 / 0 / 0 | 28.72 | 29.59 | 12:24:51 | 71G | 23.71 24.75 35.26 |
| `fill-files-final` | 132 / 0 / 0 / 0 | 15.44 | 15.96 | 12:26:16 | 71G | 13.91 21.41 32.98 |
| `route-files-final` | 247 / 0 / 0 / 0 | 118.69 | 119.27 | 12:26:32 | 71G | 16.77 21.60 32.79 |
| `remaining-1-final` | 348 / 0 / 0 / 0 | 111.44 | 111.98 | 12:28:32 | 71G | 11.93 18.55 30.18 |
| `remaining-2-final` | 708 / 0 / 0 / 0 | 75.29 | 75.83 | 12:30:24 | 71G | 19.48 19.55 29.11 |
| `remaining-3-final` | 251 / 0 / 0 / 0 | 91.24 | 91.93 | 12:31:41 | 71G | 20.23 19.70 28.35 |
| `contract-final` | 437 / 0 / 0 / 0 | 21.77 | 22.40 | 12:33:13 | 71G | 22.52 21.20 28.08 |
| `no-models-final` | 91 / 0 / 0 / 0 | 1.56 | 2.03 | 12:33:35 | 71G | 17.77 20.17 27.51 |
| `adjacent-fake-final` | 35 / 0 / 0 / 0 | 21.80 | 22.24 | 12:33:37 | 71G | 17.77 20.17 27.51 |
| `adjacent-unit-final` | 422 / 0 / 0 / 0 | 30.52 | 30.89 | 12:34:00 | 71G | 14.39 19.19 26.95 |

`new-before`, `new-after` and `new-after-final` select the eight tests named in the findings table. The seven law and witness tests passed in all three runs. The initial red run failed at the intended query assertion. The first post-fix run printed one xdist teardown warning from Hypothesis's GC callback and exited 0. `partial-model-before` ran only `tests/unit/test_fill_evidence.py::test_snapshots_query_fill_rows_only_when_requested` after adding a providerless fixture and before guarding against it.

Run arguments by label. The command passes each module list as `@$SF/<label>.txt`. Its `-final` run uses the same file list.

- `fill-files` in `tests/unit/` used `test_scheduler_fill.py`, `test_policy_fill.py`, `test_fill_routing.py`, `test_fill_evidence.py`, `test_replay_weekly_fill.py`.
- `route-files` in `tests/unit/` used `test_decision_horizon.py`, `test_route_check.py`, `test_store_contention_queries.py`.
- `remaining-1` in `tests/fake/` used `test_admission_liveness.py`, `test_admission_route_isolation.py`, `test_admission_stateful.py`, `test_policy_sandbox.py`, `test_probe_turn.py`, `test_provider_active_cap_fake.py`, `test_review_needs_shell.py`, `test_route_view.py`, `test_routing_tier_preferences.py`, `test_unadmittable_pin_admission.py`.
- `remaining-2` in `tests/unit/` used `test_admission_uncapped.py`, `test_capacity_steer_route.py`, `test_claude_usage.py`, `test_cli.py`, `test_lease_waiter_selection.py`, `test_picker.py`, `test_policy.py`, `test_policy_ramp.py`, `test_policy_sandbox_resolution.py`.
- `remaining-3` in `tests/unit/` used `test_provider_active_cap.py`, `test_scheduler.py`, `test_scheduler_split.py`, `test_scheduler_tier_preferences.py`, `test_scheduler_weekly.py`, `test_scoped_stream_capacity.py`, `test_unadmittable_now.py`, `test_unadmittable_pin.py`, `test_weekly_rank_vs_base.py`.
- `contract` used `tests/fake/test_state_contract.py`, `tests/unit/test_handler_refusals.py`, `tests/unit/test_invariants_index.py`, `tests/unit/test_test_module_names.py`.
- `no-models` in `tests/unit/` used `test_render_quota_projection.py`, `test_reset_credit_demand.py`.
- `adjacent-fake` in `tests/fake/` used `test_admission_latency.py`, `test_status_payload.py`, `test_launch_qos_review_r2.py`, `test_admission_held_waiters.py`, `test_admission_lease_waiters.py`.
- `adjacent-unit` in `tests/unit/` used `test_reset_credit_review_r1.py`, `test_timers_daemon.py`, `test_status_json.py`, `test_claude_cards_wiring.py`, `test_native_operations.py`, `test_status_hold_source.py`, `test_timers_busy_read.py`, `test_timers_probe.py`.

### Regression follow-up

- The first `remaining-1` run failed `test_d261_a_build_job_outside_a_repository_is_refused_with_the_way_out`. With scratch inside the assigned checkout, the fixture's workdir inherited the checkout during Git discovery and submit accepted it. I set `GIT_CEILING_DIRECTORIES` to the fixture's own root through its daemon helper policy for that one test in the final rerun (`SF_GIT_FIXTURE_CEILING=1`). It exercised the real Git refusal and passed. The tracked fixture keeps its original source.
- The first `adjacent-unit` run failed 157 cases because some policy fixtures supplied model IDs without providers. The gate assumed a provider key and raised `KeyError` in the timer snapshot. The supplemental query-spy case reproduced it. `fill_on` now skips model entries without a provider, and the final unit run verifies the repaired timer and status paths.
- All final module groups passed with zero skips. The PR, store and contract groups passed 2,123 tests. The two no-model modules and both adjacent groups added 548 tests for 2,671 final module passes.

### Text corrected in place

- **Order and policy.** Updated the shipped map to reset for both providers, pointed to “Why reset ships,” and removed the redundant banner.
- **Invariants and tests.** Updated the scheduler file's case and example counts and named `test_default_decisions_equal_frozen_release217`, which covers shipped and omitted defaults for both providers.
- **Mutants.** Scoped six assertion kills to the first build, pointed to the eight in Default fix r2 and the thirteen in this round, and gave the Codex omission test its current `[codex-astra-omitted]` ID.
- **Consumer audit.** Named `picker.rank`. Updated the scheduler, picker, route-check, daemon, capacity, policy and actions references. Described the daemon and timer query gates, put the historical snapshot fallback in past tense, and clarified the horizon effect of a noncanonical observation or a replacement that arrives in the snapshot-to-view gap. `build_view` drops a replaced canonical row when it can see the replacement at its clock.
- **Default fix r2, Policy and horizons.** Checked and updated each of the startup copy, policy load and hash, scheduler call and decision digest, stored decision digest, reservation policy check, horizon and forwarding references.
- **Default fix r2, Probe comparison.** The r1 `test_fill_equal_weekly_use_binds_the_earliest_reset` covered `[False-claude-opus]` of `test_equal_weekly_headroom_binds_the_earliest_reset`. Review fix r2 names that case `[account]` and adds `[model-scope]` to cover `[True-claude-opus]`. The preserved `2026-11-08-fill-first-default-r2-probe.json` still names `test_fill_only_latest_applicable_open_windows_contribute_at_any_age`.
- **Default fix r2, Proposed PR body.** Corrected the weekly and five-hour reserve direction. A report-wide search found one inverted sentence.
- **Acceptance contract.** Extended the first D-FILL1 change-log entry in place. C-11.11 states the measured-key and probe rule, and milestone row 3 points to the separate fill order. The check found 261 unique normative clause IDs. All 370 change-log bullets retain their positions, including the existing duplicate from PR #198, and the edit creates no gap. The other D-FILL1 entry, C-11.3 and the desktop integration sentence keep their text.

### Hub PR body draft

- “The daemon reads fill's evidence through its own store query, `Store.fill_reading_candidates`, so a newer unknown or null reading can't hide provider use that still belongs to an open window.” Qualify the call with the live policy. Daemon route and operator snapshots and the timer status snapshot call it only when a configured model's provider uses fill. The separate evidence still preserves a usable reading behind those masks.
- “Each of the eight mutants in `tools/fill_rank_mutations.py` fails its named test by assertion (`docs/reports/2026-11-08-fill-first-mutants-default-r2.json`). They sort weekly use ascending, put a 120 s TTL on fill's readings, count expired windows, drop the expiring tier or the desktop term, and make fill the default for Codex, for Claude or in the shipped file.” Eight is the Default fix r2 result. The review r1 run recorded thirteen assertion kills. It adds in-flight, equal-use binding reset, future observation, unknown-model nonfirst scope and Codex horizon checks. The evidence is `docs/reports/2026-11-08-fill-first-mutants-review-r1.json`.
- “The report maps each of the 307 to a test that still exercises fill under the shipped map: 233 select fill, 54 run both orders, and 20 rely on a named test that covers the same law under fill (`docs/reports/2026-11-08-fill-first-default-r2-probe.json`).” The historical counts still apply. Cite the corrected report for the equal-use binding tie witness. The historical JSON carries the old witness.
- “At `0ab7eed83`, this head's first parent, every run passed.” Identify `0ab7eed83` as the Default fix r2 head and keep its counts attached to that SHA.
- “On this head, two local runs passed 2,115 tests across `tests/fake/test_state_contract.py`, `tests/unit/test_invariants_index.py`, `tests/unit/test_test_module_names.py`, `tests/unit/test_handler_refusals.py`, `tests/unit/test_store_contention_queries.py` and all 35 test files this PR changes.” Attribute those counts to the reviewed head `7f344288a`. The final PR, store and contract runs cover 2,123 passing tests.
- “This PR still awaits an independent review.” Review r1 approved `7f344288a`. The hub owns the scoped r2 review after collection and the #318 merge sequence.

### Left for an issue

Review fix r1 addressed its ten findings as ruled. Review fix r2 adds the follow-up witnesses. The common `UNION ALL` fallback still scans `readings` for noncanonical timestamps and, under fill, future observations. The measured rewrite closes the requested plan investigation through the covering key scan and indexed seeks.

### Commands and deviations

- `date -u +%FT%TZ`, `git rev-parse HEAD` and `git status --short --untracked-files=all` recorded the start and clean checkout. I read the review and probe texts, report passages, contract clauses and hub draft with `cat`, `sed -n` and `rg`. The release reads and merge attempt are recorded above. Setup `df -H /` showed 83 GB free and `uptime` showed loads 97.34, 71.94 and 52.17.
- `getconf DARWIN_USER_TEMP_DIR` selected the requested scratch parent. `mkdir -m 700` there failed with `Operation not permitted`. I created `$PWD/build/sf-fill-fix1` with mode 700 and `$SF/t`, then set `TMPDIR` and `HYPOTHESIS_STORAGE_DIRECTORY` under `$SF` for the runs.
- `uv sync --group dev` failed to initialize the existing shared cache with `Operation not permitted`. A retry with `$SF/uv-cache` installed the dev packages on CPython 3.15.2. `uv run --no-sync --with 'pytest-xdist>=3.6' pytest --version` confirmed pytest 9.1.1. The test runs reported xdist 3.8.0 and Hypothesis 6.168.1.
- `PYTHONPATH="$PWD" uv run --no-sync python "$SF/fill_timing.py"` ran the baseline. I ran the same command with `--force-fill` after the gate and used `PYTHONPATH="$PWD" uv run --no-sync python "$SF/fill_query_experiment.py"` for the paired SQL trial. All three exited 0. The measurement tables record the load, available disk and plans. The baseline warm-up duration is unavailable.
- `git diff --name-only 53a7bac93d01afd9165588bcaf789dae75540757 HEAD -- 'tests/*test_*.py'` returned 35 files. I checked available space and `uptime` before each test run. The mutant wrapper also sampled each child. `du -sm` recorded space after every run. Each ordinary module group used up to four xdist workers.
- `uv run --no-sync python tools/fill_rank_mutations.py --basetemp "$SF/pytest-mutants-fix1" --output docs/reports/2026-11-08-fill-first-mutants-review-r1.json` exited 0. The parent started at 11:24:06 UTC with 73 GB free and loads 119.75, 95.46 and 74.17. The final rerun used `--basetemp "$SF/pytest-mutants-fix1-final"` and refreshed the same output file. It started at 12:25:21 UTC with 71 GB free and loads 19.72, 23.70 and 34.52, and exited 0.
- `git diff --check`, Python compilation and the mutation pattern check passed. `sed -n` verified the source citations. Checks against `git show HEAD:<path>` verified the historical JSON, shipped policy, store schema, frozen evaluator, comparator and equality functions. The contract check found edits at lines 9, 611 and 708 only.
- I tried named-file staging for the tests, skip, SQL rewrite, mutation tool and evidence, text corrections, and this report. Each failed with the shared Git `index.lock` denial. No staging attempt reached `git commit`.
- The full suite belongs to CI. The index-specific migration, plan and offline runs apply to schema changes. The rewrite uses the existing index.

### Scratch and final state

`$SF` is the ignored `build/sf-fill-fix1` directory in the assigned worktree. It held the timing and reporting drivers, logs, argument files, `$SF/t`, Hypothesis storage, the local uv cache and the named `pytest-*` basetemps. The pytest scratch plugin redirected kept failure state into `$SF/t`, removed completed `tmp_path` directories and reclaimed completed generated-example directories from the named fake-daemon laws. Each measurement deleted its `timing/` SQLite store. Each ordinary run removed its basetemp immediately. The mutation parent removed its child basetemps together at exit.

The largest sampled scratch use was 25 MiB excluding the local uv cache. The last `du -sm` recorded 34 MiB under `$SF`, including 33 MiB in that cache. I did not separately measure cumulative device writes.

`test_weekly_rank_vs_base.py` created eight archive directories under `$SF/t`. The two runs removed `sf-64b4175f-6juos_rx`, `sf-64b4175f-sd_tvmct`, `sf-f3bffcea-0hzd8brg`, `sf-f3bffcea-hmud7ds6`, `sf-f3bffcea-n8ednuau`, `sf-f3bffcea-p_wgqnwn`, `sf-f3bffcea-r_ipo9yb` and `sf-f3bffcea-sztngav9`.

At 12:34:54 UTC, `python3 -B "$SF/cleanup.py"` inventoried Darwin temp and used `shutil.rmtree` with a guard on the `sf-fill-fix1` path component. It removed `$SF`, including test homes, logs, scratch scripts, Hypothesis storage, scratch bytecode and the local uv cache, then removed the empty `build/` parent. It also removed generated `__pycache__` directories in `subfleet`, its `adapters`, `conversations`, `gate`, `guard` and `sessions` packages, `tests`, `tests/unit`, `tests/fake`, `tests/fixtures/fill_first` and `tools`. The direct Darwin temp check found zero associated directories. It left 24 unrelated matching entries alone. No worktree-root `.hypothesis` or `.pytest_cache` appeared.

I left the 17 MiB `.venv` in the worktree as requested and verified its imports after cleanup. The shared uv cache remains in place. Final free disk was 71 GB and load averages were 11.32, 17.69 and 25.91. The assigned checkout retains the tracked edits and the new mutation JSON for salvage.

Head built: 7f344288ac32fba20f10154379493d6fc339abe9

## Review fix r2

Started at 2026-11-28 02:45:29 UTC. The detached checkout matched `0da60cf72bfc361544b26ea4fcd69644e1217695`, the head approved in review r2, and both the tracked status and the untracked list were empty.

| Finding | Coverage added | Mutation evidence |
| --- | --- | --- |
| P2. Binding reset at equal weekly use | The original account case and the new Opus-scope case each bind the +1 h reset, mark `claude-1` expiring and choose it. In the new case both accounts reset at +3 d and `claude-1` also has an Opus-scoped row at equal use resetting at +1 h. | `binding-reset-dropped` and `binding-scope-before-reset` each failed by assertion in `test_fill_equal_weekly_use_binds_the_earliest_reset[model-scope]`. |
| P3. Last configured model in an unknown-model pick | `test_unknown_model_pick_binds_each_configured_claude_scope` takes its model names from the Claude entries in the configured policy. Each case ranks the lane with 0.2 account use and 0.9 scoped use ahead of the lane with 0.5 account use and asserts 0.9 weekly use. | `picker-fill-drops-last-model` failed by assertion in the `[haiku]` case, the last model in the current fixture order. |
| P3. Timer snapshot query gate | `test_timer_snapshot_queries_fill_rows_only_when_requested` uses a real `Timers` instance and a store query spy. The shipped reset snapshot makes zero fill queries and the Claude fill snapshot makes one at the snapshot clock. | `timers-fill-ungated` failed the zero-call assertion. |

All 17 mutant children exited 1 with one assertion failure each. The existing set comprises `weekly-ascending`, `ttl-only`, `expired-counts`, `expiring-dropped`, `desktop-dropped`, `default-codex-fill`, `default-claude-fill`, `shipped-default-claude-fill`, `in-flight-dropped`, `binding-latest-reset`, `horizon-fill-future-dropped`, `picker-fill-first-model-only`, and `horizon-fill-claude-only`. Each replacement matched exactly once. SHA-256 checks verified restoration, and byte comparisons matched the base for the three historical mutation JSON files. [Review r2 mutation evidence](2026-11-08-fill-first-mutants-review-r2.json)

The witness list maps `[False-claude-opus]` to `[account]` and `[True-claude-opus]` to `[model-scope]`. The r1 findings table and probe summary now identify r1's account-first coverage and r2's added scope-first case. The scheduler case count is 43 and the mutant summary identifies the six, eight, thirteen and seventeen results by round.

### Ordered verification

| Run | Result | Elapsed |
| --- | --- | ---: |
| Targeted cases, test-only diff | 7 passed | 71.17 s pytest |
| Full mutation run | 17 assertion kills | 1,487.05 s wall |
| Five fill modules | 138 passed | 424.33 s pytest |
| Four routing and picker modules | 96 passed, 4 failed | 1,687.82 s pytest |
| Five timer reader modules | 256 passed, 1 failed | 469.70 s pytest |
| Four contract and index modules | 437 passed | 257.08 s pytest |
| Focused routing and timer retry | 5 passed | 894.67 s pytest |

All four routing failures originated in pytest's 540 s timeout while Hypothesis was generating or processing examples in `test_route_check.py`. Three also produced a secondary Hypothesis strategy error after interruption. The timer failure was `test_usage_timeout_does_not_block_cycle_or_shutdown`, whose cycle took 1.17 s against a 0.4 s assertion. Both modules match the base. Across the module runs and focused retry, all 932 requested module cases passed.

### PR-body sentences for the hub

- “Each of the thirteen mutants in `tools/fill_rank_mutations.py` fails its named test by assertion (`docs/reports/2026-11-08-fill-first-mutants-review-r1.json`).” Use 17 and the review-r2 evidence path for the current result.
- “The other five drop the in-flight key, bind the later reset at equal use, stop the horizon noting a future reading, scope an unknown-model pick to its first model only, and apply the horizon's fill rule to Claude alone.” The current list also covers dropping reset from the binding key, sorting scope before reset, dropping the last picker model and removing the timer query gate.
- “`docs/reports/2026-11-08-fill-first-default-r2-probe.json` holds the probe's run, and the report names the current test for the equal-use reset tie.” Name the two variant-specific witnesses above. The probe counts describe the historical run.
- “This head closes all ten:” Scope the ten findings to the r1 rulings and record the three r2 coverage findings separately.
- “New tests pin four keys that no test covered: fewer jobs in flight at equal weekly use, the earliest binding reset, the horizon's clock for a future reading, and an unknown-model pick's scopes past the first model.” State that r1 covered the account-first reset and Opus pick, and that r2 adds the scope-first reset, every configured Claude pick scope and the timer snapshot gate.
- “A scoped review of this round's changes.” Identify the remaining scoped review as r3. Review r2 approved the assigned base.

The “On this head, the fix round's PR, store and contract runs passed 2,123 tests” and “The hub's run on this head passed 531 tests” statements describe r1 evidence at `0da60cf72bfc`. Anchor them to that SHA when adding the r2 counts.

### Setup and deviations

Darwin temp denied creation of `$T0/sf-fill-rfix2`, so the runs used the worktree fallback `build/sf-fill-rfix2`. The shared uv cache also denied writes. The import probe failed in the new environment, and `uv sync --group dev` succeeded with `$SF/uv-cache`. The runs used `$SF/t` for `TMPDIR` and `$SF/hypothesis` for Hypothesis storage. A scratch-only pytest hook redirected retained failure state into `$SF`, and Git discovery from test fixtures stopped at the scratch root.

An ephemeral directory disappeared during a `du` sample in the fill group. The sampler exited 1 after pytest printed 138 passes. I recorded the final 4 MiB size, removed that basetemp and corrected sampling for the later groups. The mutation parent and every mutant child had a separate disk and load check. The focused retry used two workers to reduce concurrency and kept the 540 s timeout and configured Hypothesis examples.

The largest recorded scratch peak was 54 MiB excluding the uv cache. The fill peak is unavailable, and cumulative writes were unmetered. I removed each basetemp and the three archived-baseline trees, then used a guarded `shutil.rmtree` on the exact worktree fallback path. The final scratch size was 22 MiB including its uv cache. The worktree retains the 11 MiB ignored `.venv`. The checks found zero owned matching directories directly under Darwin temp and zero worktree-root `.hypothesis` directories.

The sandbox denied Git's `index.lock` on named-file staging. The test-only run therefore used the worktree diff at the assigned HEAD. The three tests, mutation tool, new JSON and this report are available for Subfleet salvage.
