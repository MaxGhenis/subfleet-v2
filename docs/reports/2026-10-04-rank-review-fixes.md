# PR #120: round-two review fixes

The review was read in full, including both patches, at `/Users/maxghenis/reviews/subfleet-2110/rank-expiring/review-r2.md`. Work starts at `7bab08169a0f` against release base `f832e6b8`. All changes are confined to the assigned worktree. No push, live-state write, daemon intervention, credential-based endpoint request, sub-agent, detached test command, or process-pattern kill was used.

## Findings and regression evidence

| Finding | Fix | Regression and original-head result | Mutation proof |
| --- | --- | --- | --- |
| P1: renewal uncertainty changes admission/probes/pick | [scheduler.py:652](../../subfleet/scheduler.py#L652) separates admission `measured` from `ranking_measured`; [scheduler.py:899](../../subfleet/scheduler.py#L899) uses the latter only for ordering; [picker.py:155](../../subfleet/picker.py#L155) aggregates ranking evidence without replacing admission freshness. | The reviewer's full [differential:142](../../tests/unit/test_weekly_rank_vs_base.py#L142) compares admission verdicts with actual `f3bffcea` and `64b4175f` source, including stopped-window histories. On `7bab0816`: admission comparisons pass (2); probe and picker properties fail (8). Fixed: all 10 pass, 3,000 configured generated examples. | Coupling admission to ranking produces three assertion failures; restoring eternal stopped-window uncertainty produces four. |
| P1: a window no longer reported keeps demoting/probing/excluding | [scheduler.py:584](../../subfleet/scheduler.py#L584) bounds renewal uncertainty and explanatory recency by the observation time and TTL. [capacity.py:313](../../subfleet/capacity.py#L313) records observation/expiry clocks, including stale-labeled evidence. | [Stopped Claude-9 regressions:202](../../tests/unit/test_weekly_rank_vs_base.py#L202), four provider/scope cases at `:250`, renewal expiry at `:291`, and two stale-recency horizon cases at `:310`. All nine fail on `7bab0816` and pass with the fix. | Eternal uncertainty: 4 failures; using an abandoned window for age: 4; removing expiry horizon: 1; removing stale-observation horizon: 1. |
| P2: busy Claude reads prolong idle unavailability | [timers.py:498](../../subfleet/timers.py#L498) defers busy reads; [timers.py:841](../../subfleet/timers.py#L841) pays inherited pacing debt before acquiring idle leases, publishes/releases all idle homes, then starts busy reads at `:892`. | [Availability:1155](../../tests/unit/test_timers_busy_read.py#L1155): Claude/Codex idle lanes, both enrollment orders, eight paced busy lanes; plus prior-cycle pacing debt at `:1179`. All five fail on `7bab0816` and pass with the fix. They inspect real temporary-store leases, readings and dispatchability. | Reading busy lanes inside the idle phase produces four assertion failures; omitting the initial pacing wait produces one. |
| P2: unavailable Claude usage sensor | [timers.py:563](../../subfleet/timers.py#L563) preserves old readings and persists a separate completion-based sensor cooldown; [timers.py:105](../../subfleet/timers.py#L105) restores it; `:559` gates repeat reads. `:571` retains fractional timestamp precision. | [Missing-sensor tests:1202](../../tests/unit/test_timers_busy_read.py#L1202) exercise idle/busy failures, repeated requests, restarts and 3,600-second Retry-After; `:1233` covers fractional cadence and deadline boundaries. On `7bab0816`: 10 fail, 2 pass; fixed: 12 pass. The two original passes cover already-working integer Retry-After behavior. | Bypass cooldown, forget durable cooldown, truncate deadline, and fake provider freshness all produce assertion failures. |
| P3: insufficient replay caveats | [PR body](2026-10-03-rank-earliest-reset.md) states calibration error, changed placements, three-attempt gain, provider split, Claude-only five-hour column, unavailable sensor and historical-implementation limitation. | [Report tests](../../tests/unit/test_rank_review_report.py): all three fail against the original report and pass against the revised body. | Removing calibration, overstating gain, claiming Claude freshness, or expanding the five-hour column to both providers each fails an assertion. |
| P3: weekly equal-headroom tie coverage | [Tie regression:224](../../tests/unit/test_weekly_rank_vs_base.py#L224) tests both providers and both scope insertion orders. | All four pass on `7bab0816` and with the fix: this was missing coverage for an already-correct rule, not a runtime defect. | Reversing the binding-reset tie-break produces four assertion failures. |
| P3: inconsistent renewed-lane `why` | [cli.py:1141](../../subfleet/cli.py#L1141) reports admission freshness, ranking freshness, pending renewal and admission headroom explicitly, preserving the existing reserve prefix. | [Why regression:269](../../tests/unit/test_weekly_rank_vs_base.py#L269) checks ages 0, 30 and 119 seconds. All three fail on `7bab0816` and pass with the fix. | Hiding pending renewal produces three assertion failures; re-coupling measured status also produces three. |

The baseline runner compiles the complete actual `7bab0816` production modules in memory; it does not approximate the old implementation. The differential independently archives `f3bffcea` and the earlier admission checkpoint. Production source is never changed for baseline or mutation runs. Four already-correct tie cases, two unchanged admission comparisons and two integer Retry-After cases are explicitly reported as original-head passes rather than claimed regressions.

All **46 new regression/coverage cases pass**. On the original head, **38 fail and eight pass** for the reasons above. The final ranking/differential file passes all 26 tests in 35.914 s. [Structured baseline and mutation proof](2026-10-04-rank-review-proof.json) retains the failed baseline node names and every accepted mutation result: **27/27 killed** (17 review-specific, ten existing ranking-law mutations).

## What the daemon recorded

[Read-only audit](2026-10-04-recorded-usage-audit.json), cutoff `2026-10-04T10:33:46Z`: the replay week contains 1,757 sensor verdicts carrying `probed_at`: 997 rate-limited (996 with 3,600-second Retry-After), 708 no-scope, 20 identity-unbound, six network errors, one unavailable and 25 unknown. There are zero successful Claude usage reads. Since cutover `2026-10-04T02:08:33Z`: 18 no-scope and 39 rate-limited, all 39 with one-hour Retry-After, again zero successes. The entire store has three `oauth-usage` window rows at one distinct observation time, `2026-09-18T17:48:01Z`. Current Claude freshness is supplied by attempt-end events. There are 73,168 Codex reading rows and no five-hour window. `daemon.log` contains no endpoint status lines; structured readings/verdicts supply the evidence. Different snapshot intervals and filters explain why these counts are not equated with the reviewer's totals.

[audit_recorded_usage.py](../../tools/audit_recorded_usage.py) uses SQLite URI `mode=ro`, `PRAGMA query_only=ON`, a short read transaction and a progress timeout, and aggregates the log without printing credentials or raw payloads. It never calls an endpoint. The failed-sensor fix does not invent successful readings or newer observation times.

## Reproduction

Dependencies are locked: CPython 3.12.14, pytest 9.1.1 and Hypothesis 6.168.1. [run_rank_review_slice.py](../../tools/run_rank_review_slice.py) records exact node outcomes, arguments, seeds and elapsed times in [test-results.json](2026-10-04-rank-review-test-results.json). Properties configured for 1,500 examples are run as three foreground slices of 500 examples with seeds 1, 2 and 3; only the per-slice budget changes, never an assertion. Other properties retain their configured budgets.

Across **724 unique tests**, final outcomes are **720 passed, four sandbox failures, zero skipped**. All requested suites ran in **40 foreground slices**, with a longest slice of **351.894 s**; none reached ten minutes. The outcome artifact retains intermediate route assertion failures and their successful reruns. Totals use the latest outcome for each node, never add repeated seeds or follow-ups as extra tests. An accidentally changed independent reading-age test was restored in `08948d6b`; the renewal-expiry expectation was then corrected without weakening full decision equality. Compilation and `git diff --check` pass. No task process remains running.

| Suite | Unique final results | Configured generated examples / relevant slice time |
| --- | ---: | --- |
| Decision horizon | 7 passed | 600 examples; final strengthened horizon slice, including two review regressions: 179.525 s |
| Scheduler and turns | 107 passed | 49.071 s |
| Scheduler/reference split | 3 passed | 4,500 examples; longest shared slice 266.181 s |
| Weekly | 33 passed | 6,800 examples; longest of six slices 105.987 s |
| Picker | 39 passed | Shared policy/picker/horizon/report slice: 210.734 s |
| Route check | 24 passed | 5,000 examples; longest slice 289.132 s |
| Policy | 154 passed | Shared core slice: 210.734 s |
| Busy reads | 115 passed | Initial 113: 351.894 s; final 12 backoff cases (overlap included): 129.090 s |
| Timer probes, Codex probes, Claude usage | 118 passed | 35 + 55 + 28 tests; 55.804 s |
| Admission | 27 passed | 4,250 examples across six slices; longest 189.268 s |
| Fake admission priority | 24 passed, 4 sandbox failures | 47.726 s; all four reproduce on `7bab0816` |
| Other fake picker/routing/busy-admission/probe/pin suites | 40 passed | 71.450 s |
| Ranking review differential and deterministic regressions | 26 passed | 3,000 generated examples + 16 deterministic cases; 35.914 s |
| PR body assertions | 3 passed | Shared core slice |

Mutation checks also ran in foreground batches below ten minutes. The final five review-specific checks took 27.60 s; the two five-mutation weekly batches took 112.61 / 21.69 s. Every mutation name and assertion count is in the proof JSON; the weekly checks stop at their first real failing assertion. No import or compilation error is counted as a kill.

```sh
.venv/bin/python tools/review_fix_mutations.py --baseline differential
.venv/bin/python tools/review_fix_mutations.py --baseline ranking
.venv/bin/python tools/review_fix_mutations.py --baseline busy
.venv/bin/python tools/review_fix_mutations.py --baseline report
.venv/bin/python tools/review_fix_mutations.py --baseline fake
.venv/bin/python tools/review_fix_mutations.py --only NAME
.venv/bin/python tools/weekly_rank_mutations.py --help
.venv/bin/python tools/run_rank_review_slice.py --examples 500 --seed 1 NAME TEST_NODE
```

The mutation runner accepts a kill only when pytest exits 1 with failed tests and an assertion failure. Inapplicable edits, import errors and compilation failures do not count. One initial fake-freshness mutant had an indentation error; it was corrected and re-run, and only its ten actual assertion failures count.

Four fake admission-priority tests cannot pass in this sandbox. [test_admission_priority.py:57](../../tests/fake/test_admission_priority.py#L57), `:71` and `:85` raise `PermissionError: Operation not permitted: '/bin/ps'`. The reused-PID case at `:179` cannot verify its caller and lacks the expected background hold. The fixed suite gives 24 passed / 4 failed in 47.726 s. Compiling the actual `7bab0816` modules and running the entire same suite gives the identical 24 passed / 4 failed (8.62 s pytest, 10.26 s wall). Tests, assertions, liveness logic and sandbox permissions are unchanged; these four failures are reported separately from the review regressions.

## Commits and delivery

Coherent commits, each with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`:

- `1337e343`: isolate ranking uncertainty from admission, extend the differential and explain renewal.
- `08948d6b`: restore the independent reading-age invalidation assertion accidentally changed during an earlier edit.
- `21f01ab4`: separate idle and busy sensor phases and persist failing-sensor cooldowns.
- `4f65c514`: account for renewal uncertainty expiry in route horizons.
- `37ab3fd0`: preserve full precision in sensor retry deadlines.
- `da674702`: bound explanatory recency and add its independent observation/expiry clocks.

The sandbox rejects writes to shared Git metadata. Commits use workspace-local `.git-local` on `fix/pr120-review-r2`, with the original `.git` pointer and caller checkout preserved. These commits are followed by the verification/report commit and the artifact-storage commit, with the same co-author trailer. The delivery bundle is [2026-10-04-rank-review-fixes.bundle](2026-10-04-rank-review-fixes.bundle), prerequisite `7bab08169a0f16c277c2ab9d89e9583174b57127`, named head `refs/heads/pr120-review-fixes-bundle`. Its exact head is obtainable with `git bundle list-heads`. Delivery requires `git bundle verify`, an import into a separate workspace-local bare repository, and confirmation of the imported head/tree. The artifact-storage commit follows the bundle head because a bundle cannot contain itself.
