# Claude fill-first ranking (D-FILL1)

Implemented from `266c3ab8b0be2548fbe0abc77273fa1ccd7fa9a9`, in the assigned isolated workspace. The sandbox denies the shared Git `index.lock`: every named-file staging attempt failed, so no commit or branch switch could be made. The files remain available for Subfleet salvage. Nothing was pushed, no history was rewritten, and no caller worktree file or live state was read or changed.

## Order and policy

`admission.lane_order` defaults to `{"claude": "fill", "codex": "reset"}`. An omitted provider in an explicit map uses `reset`. `admission.expiring_s` accepts positive whole seconds or null, defaults to 21600, and null disables expiry preference. `admission.lane_spread` accepts its existing positive integer/null scalar or a provider map; omitted map providers use 2. The default remains 2. Validation names the offending key, including provider entries.

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

`tests/unit/test_scheduler_fill.py` contains 38 cases with 4,100 configured Hypothesis examples. The frozen full base evaluator permits complete decision/error comparisons without Git history at test time. Its SHA-256 is `f6c9ad0cae11fd8b9d624f856b0782ddb7e155cbc4dfca1ab85ce141d1da7d54`; extracted `rank_key` is `1e100b7a9681f7bf7dc19ebf5d8e701785ce3fb8d5d0d57d5041e45f88bc6a54`, and `ranking_usage` is `ade25eea6e4bdbed6f569d7812bafbd64c1c3a093bbf87341d01d338083a7e21`. The frozen bytes were checked against `git show 266c3ab8b0be:subfleet/scheduler.py` while building. Both evaluators receive the same policy, view and job, including the input policy hash; changed default-policy bytes naturally have a different hash.

| Invariant | Test in `test_scheduler_fill.py` |
| --- | --- |
| Same candidates and exact refusal predicates, only ranking changes | `test_fill_and_reset_have_identical_candidates_and_refusals`; `test_fill_complete_chain_has_identical_candidates_and_rejection_predicates`; `test_fill_keeps_the_same_probe_predicate_for_each_pinned_lane` |
| Reset equals complete frozen-base decisions/errors; default Codex equals base | `test_reset_decisions_equal_frozen_release217`; `test_reset_complete_chain_decisions_and_errors_equal_frozen_release217`; `test_default_codex_decisions_equal_frozen_release217` (shipped and omitted defaults); `test_frozen_release217_ranking_sources_have_verified_sha256` |
| Desktop last; affinity leads its class | `test_fill_desktop_last_and_turn_affinity_leads_its_class` |
| Higher weekly use cannot rank later with all earlier tiers equal | `test_fill_more_weekly_use_never_ranks_after_less_in_equal_earlier_tiers` |
| Only latest applicable open windows contribute, otherwise zero | `test_fill_only_latest_applicable_open_windows_contribute_at_any_age`; `test_fill_keeps_old_open_provider_reading_and_ignores_expired_one` |
| Expiring first, earlier expiry first; null disables | `test_fill_expiring_precedes_nonexpiring_and_earlier_expiring_reset_wins`; `test_fill_null_expiring_disables_the_tier`; `test_fill_expiring_reset_order_keeps_fractional_seconds` |
| Unique total keys; lane/reading permutations cannot change order | `test_fill_keys_are_unique_total_and_order_independent`; `test_fill_equal_observation_ties_do_not_depend_on_reading_order` |
| Sequential packing | `test_fill_packs_a_prefix_of_starting_use_when_expiry_and_bands_are_off` |

The precise packing claim: 2–7 initially eligible Claude lanes, initial weekly use from 0 through 0.9 (ties allowed, lane id breaks ties), identical still-open weekly resets, five-hour use 0.5, and equal desktop/affinity/stranded keys. The band and expiring tier are disabled. Each of 1–50 sequential jobs adds a fixed 0.001–0.3 weekly use, capped at 1; no window resets during the simulation. At every step, lanes ever selected form an initial segment of the lanes sorted by (-starting weekly use, lane id). Before a fresher lane first receives work, every preceding lane is either currently refused at exhaustion or strictly above 0.98 weekly use. The property does not assert a prefix across different earlier keys or expiry priorities, or arbitrary external refusals.

Additional generated tests cover bounded store candidates versus full histories (100 examples), cached-route expiry/reset equality (200 examples), and policy values (150 examples). Explicit tests cover both reserve complements at equality and one float above, per-provider bands, stranded precedence, auxiliary evidence masks, noncanonical/future timestamps, override restoration, isolated default maps, conservative pick freshness, unknown-model pick scope and CLI output.

## Mutants

`tools/fill_rank_mutations.py` applied each mutant alone to a pristine file copy, ran its named test, restored the original bytes in `finally`, and verified SHA-256. No Git checkout was needed. All six died by assertions, with no collection/import errors counted as kills. Evidence: [2026-11-08-fill-first-mutants.json](2026-11-08-fill-first-mutants.json).

| Mutant | Killing test in `test_scheduler_fill.py` |
| --- | --- |
| Weekly use ascending | `test_fill_more_weekly_use_never_ranks_after_less_in_equal_earlier_tiers` |
| Only readings under the 120-second TTL | `test_fill_keeps_old_open_provider_reading_and_ignores_expired_one[121]` |
| Expired window still counts | `test_fill_keeps_old_open_provider_reading_and_ignores_expired_one[121]` |
| Expiring tier dropped | `test_fill_expiring_precedes_nonexpiring_and_earlier_expiring_reset_wins` |
| Desktop dropped from fill | `test_fill_desktop_last_and_turn_affinity_leads_its_class[claude-opus]` |
| Default Codex follows fill | `test_default_codex_decisions_equal_frozen_release217[omitted]` |

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
