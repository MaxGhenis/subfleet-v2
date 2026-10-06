# 2026-10-03: earliest weekly reset routing (Subfleet 2.1.10)

Round-two review fixes start at `7bab0816`, against release base `f832e6b8`. Renewal uncertainty now affects ranking only and expires at the reading TTL; busy sensor reads run after idle publication and lease release; failed Claude sensor cooldowns survive requested repeats and restarts. The current finding-by-finding proof and delivery details are in [2026-10-04-rank-review-fixes.md](2026-10-04-rank-review-fixes.md). Nothing was pushed or changed in the live daemon.

Current review verification: all 46 new regression/coverage cases pass (38 fail on the original head; eight already-correct cases pass there), and 27/27 mutations are killed by assertions. The requested suite scope has 724 unique tests: 720 passed and four fake admission-priority sandbox failures that reproduce exactly on `7bab0816` because `/bin/ps` is unavailable. All 40 foreground suite slices stayed below ten minutes, longest 351.894 s. The historical verification and replay below are explicitly separated from this evidence.

Original implementation history: `feat/rank-earliest-reset` started from checkpoint `3e31ab7bd5bf06efbd5aa61592a542e2c7bf4d3d`. Runtime commits were `5f6a1e4b` (weekly routing, evidence and probe freshness), `4f0a05a0` (unknown-model picker evidence/order), and proof/replay commit `5110e5d4`. Every implementation and review-fix commit carries `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

The exact ascending comparator is [scheduler.py:881](../../subfleet/scheduler.py#L881), contracted in [acceptance-contract.md:231](../acceptance-contract.md#L231):

1. `desktop`: false first, so an eligible desktop lane is always last.
2. For turns only, `lane_id != affinity_lane`: affinity first among the preceding desktop class.
3. `in_flight // admission.lane_spread`, in the job's detached/turn pool; default `2`, or band `0` for `null`.
4. Claude only: `not bool(stranded_scopes)`, retaining C-23.37's position ahead of measured status.
5. `not ranking_measured`: current ranking evidence first; admission freshness remains separate.
6. Weekly reserve flag: true only when binding weekly headroom is strictly below `admission.weekly_reserve`; false first.
7. Five-hour reserve flag: true only when the least applicable five-hour headroom is strictly below `admission.five_hour_reserve`; false first.
8. Binding weekly reset ascending; missing resets follow known resets in the same preceding classes.
9. Binding weekly headroom descending.
10. In-flight ascending, then lane ID ascending, giving unique deterministic keys for distinct lanes.

[ranking_usage, scheduler.py:559](../../subfleet/scheduler.py#L559) binds the least-headroom fresh `seven_day` window of scope `account` or the requested model ID, carrying that window's own reset. Equal headroom binds earliest known reset, then scope. Five-hour headroom is the minimum across those same applicable scopes. A latest applicable provider window observed within the TTL whose reset is at or before now makes the ranking unmeasured until another reading arrives or that window ages out. Stopped windows outside the TTL no longer demote a freshly read lane. No zero-utilization renewal reading is invented. Fresh means numeric `provider` evidence no later than now and within the configured TTL (default 120 seconds), with any reset still future. Unmeasured ranking headrooms/reset are unknown and reserve flags false. Admission's `measured`, pre-launch probes and pick exclusions continue to use their original fresh-reading predicates. Eligibility floors, closures, slots and C-11.7 model reserve retain their predicates; C-11.7 slack no longer orders candidates. The shipped `headroom_floor` remains `0.15`, the live policy was not changed, and `lane_spread` remains `2`. Reserves introduce neither refusals nor waits.

[capacity.py:271](../../subfleet/capacity.py#L271) bounds the reservation horizon by a recent window's reset and TTL expiry, including when a renewed window's uncertainty ages out beside newer weekly evidence. A stopped window outside the TTL supplies no renewal clock. [route_check.py:267](../../subfleet/route_check.py#L267) advances explanatory reading ages when unchanged lane verdicts are reused.

The defaults are [policy.py:176](../../subfleet/policy.py#L176) / [default_policy.json:161](../../subfleet/default_policy.json#L161). Both keys accept finite fractions in [0,1]. Strict boundaries compare utilization against `1 - reserve`, avoiding binary subtraction classifying exactly 90% usage as below 10% remaining.

| Reserve | Default | Evidence from observed per-job usage deltas, including observed retries |
| --- | ---: | --- |
| `admission.weekly_reserve` | `0.02` | Weekly p90 is 1.9608% for Claude and 1.6046% for Codex. Two percent covers roughly typical p90 demand; p95 is 3.3349% / 2.6363%, so it deliberately remains a preference rather than a long-job guarantee. |
| `admission.five_hour_reserve` | `0.10` | Claude five-hour p95 is 10.5614% per job and 9.7340% per attempt. Ten percent is a rounded guard near this p95; the tail reaches 45%. |

Codex supports a five-hour window: [adapters/codex.py:251](../../subfleet/adapters/codex.py#L251) reads both primary and secondary usage windows, normalizes minutes or seconds, and maps 300 minutes through `WINDOW_KEYS` to `five_hour`. Therefore the guard applies to both providers whenever that window is reported. This particular backup contains no Codex five-hour readings, so its replay cannot measure that guard's Codex effect.

[Timers._probe_lane:476](../../subfleet/timers.py#L476) reads usable idle and busy lanes each cycle, subject to credential/closure/read fences and cooldowns. [Timers._busy_read](../../subfleet/timers.py) generalizes #97 (`a5d84bbf`) to Claude's no-turn `api/oauth/usage` and Codex's no-turn `wham` usage GET. All idle homes finish their read/heal phase, publish, and release their timer leases before any busy read starts or consumes Claude pacing. Busy reads take no slot lease or heal turn and retain #97's transactional publication/limit-report fence. Claude pacing and Retry-After still apply. Failed Claude reads persist a sensor cooldown for at least the existing completion-based probe interval, extended by Retry-After; requested cycles and restarts respect it, including when a busy failure cannot publish an admission verdict. Failures keep previous provider values and observation times, so their age increases honestly. Disabled/v1/desktop, operator-held/auth-dead and revoked-epoch lanes and outstanding readers retain their existing exclusions.

The read-only audit on 2026-10-04 found **no Claude freshness gain** from this endpoint on this machine. The daemon's structured `timer.verdict` records contain zero successful Claude usage reads in the replay week; failures include HTTP 403 (`no-scope`) and HTTP 429 with a 3,600-second Retry-After. Since the daemon cutover at 2026-10-04T02:08:33Z, every recorded sensor result is still either `no-scope` or `rate-limited`. The entire store has just one distinct `oauth-usage` observation time, 2026-09-18T17:48:01Z (three window rows). Current Claude freshness comes from attempt-end events. `daemon.log` contains no endpoint status lines, so this finding rests on recorded sensor verdicts and readings, not inferred log messages. No credential was loaded and no endpoint was called for this audit. Exact counts and cutoff are saved in [2026-10-04-recorded-usage-audit.json](2026-10-04-recorded-usage-audit.json); [audit_recorded_usage.py](../../tools/audit_recorded_usage.py) uses a read-only, query-only SQLite snapshot. None of the Codex readings at that audit cutoff is a five-hour window: the code supports it conditionally, but it has not been observed here.

The audit's explicit replay interval contains 1,757 recorded sensor results: 997 rate-limited (996 with Retry-After 3,600 s), 708 no-scope, 20 identity-unbound, six network errors, one unavailable and 25 unknown; zero successful. This counts only verdicts carrying `probed_at`, excluding attempt credential verdicts and empty transaction events. At the 2026-10-04T10:33:46Z cutoff, the post-cutover results are 18 no-scope and 39 rate-limited (all 39 with the one-hour Retry-After), and there are 73,168 Codex reading rows, zero five-hour. These interval and filtering choices are stated explicitly rather than equating these totals with the reviewer's different snapshot totals.

[cli.py:1133](../../subfleet/cli.py#L1133) and [picker.py:132](../../subfleet/picker.py#L132) expose reserve class, weekly scope/reset/headroom, five-hour headroom and reading age. `pick --all` puts explanations on stderr and keeps path/email stdout; `why` lists each candidate. With no exact model, pick requires all configured family models to qualify, binds weekly evidence across them, and uses that same aggregate evidence to rank; per-model JSON details remain inspectable.

The replay used only a transactional read-only SQLite `.backup` under `/private/tmp`; the live store and running daemon were untouched. [replay_weekly_ranking.py:160](../../tools/replay_weekly_ranking.py#L160) attributes positive consecutive utilization deltas within one reset cycle by overlapping active seconds. Account and requested-model weekly windows are charged separately. Unobserved/limited demand uses successful siblings or provider/window medians. Demand is charged at admission; factual attributed increments are subtracted at reading events; ranked utilization remains fixed between sensor timestamps. The cost model assumes normalized equal account fractions, fixed recorded durations/retries/models/standing candidate exclusions, partial consumption when a modeled limit is reached, and physical renewal without inventing measured ranking evidence.

The window is **2026-09-26 15:23:38 through 2026-10-03 15:23:38 UTC**: 2,801 job creation arrivals, 3,188 recorded attempt reservations (2,544 first admissions), and 42,647 loaded usage readings including initialization. Placement uses the recorded reservation timestamps, not submission times; unadmitted jobs have no observable cost and cannot be placed in this reconstruction. All attempts have decision evidence. Actual recorded `limited` is 714/3,188 = 22.3965%, with 248 unfinished attempts in the denominator. Counterfactual placements preserve historical candidate sets/closures and turn affinity; no new retries or promotions are invented. The urgency alternative uses descending `weekly headroom / hours to binding reset` behind the same prefix and reserve guards.

| Placement rule | Unused weekly capacity sum | Within 5% of five-hour limit (Claude only) | Modeled limited attempts |
| --- | ---: | ---: | ---: |
| old | 3.747285 | 116 (3.64%) | 232/3188 (7.2773%) |
| new, spread=2 | 3.747142 | 106 (3.32%) | 229/3188 (7.1832%) |
| urgency=headroom/hours | 3.747142 | 115 (3.61%) | 283/3188 (8.8770%) |
| new, spread=4 | 3.831945 | 102 (3.20%) | 254/3188 (7.9674%) |
| new, spread=null | 4.339019 | 130 (4.08%) | 310/3188 (9.7240%) |

Unused sum is normalized account-week capacity across 13 account resets, rather than tokens or dollars. The model is **not calibrated**: its old rule predicts 232/3,188 limited attempts (7.2773%), versus 714/3,188 actually recorded (22.3965%), and moves 805/3,188 placements off the lanes actually used. The new default improves modeled limits by only **three attempts, 0.0941 percentage points** overall: Claude worsens 155→159 while Codex improves 77→70. The five-hour column covers Claude only; no Codex five-hour readings were observed. Urgency and wider/no bands perform worse overall here. These small modeled differences cannot establish a production improvement or calibrate production error rates.

New spread 2 selects measured lanes for 1,661/3,188 attempts (52.10%). Historical readings retain their original timestamps; the replay does not simulate additional successful busy reads. In particular, Claude's missing usage sensor supplies no such freshness benefit today. The replay also predates the review fixes separating admission freshness and limiting renewal uncertainty to the TTL, so its metrics describe the historical implementation, not a replay of these fixes. There are no Codex weekly resets in this snapshot. The last pre-reset observations are 2,731–553,672 seconds old; external usage since those observations is unknown. The 0.000143 account-week difference between old and new default unused capacity is negligible and is not robust evidence of reduced expiration waste.

Estimated unused capacity at each recorded account reset, in percent of that account week, follows. The disabled/enabled aliases of one Claude account are deduplicated. Every observed reset is Claude.

| Account lane | Reset UTC | Pre-reset reading age (hours) | Old | New 2 | Urgency | New 4 | New null |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| claude-3 | 2026-09-26T17:00:00Z | 17.58 | 1.0000% | 1.0000% | 1.0000% | 1.0000% | 1.0000% |
| claude-2 | 2026-09-27T00:00:00Z | 72.72 | 1.0000% | 1.0000% | 1.0000% | 1.0000% | 1.0000% |
| claude-14 | 2026-09-27T11:00:00Z | 95.25 | 56.0000% | 56.0000% | 56.0000% | 56.0000% | 56.0000% |
| claude-10 | 2026-09-29T04:00:00Z | 61.27 | 10.0000% | 10.0000% | 10.0000% | 10.0000% | 10.0000% |
| claude-5 | 2026-09-29T05:00:00Z | 60.42 | 12.7199% | 12.7199% | 12.7199% | 20.0000% | 20.0000% |
| claude-12 | 2026-09-29T19:00:00Z | 104.21 | 71.0000% | 71.0000% | 71.0000% | 71.0000% | 71.0000% |
| claude-18 | 2026-10-01T16:00:00Z | 0.76 | 22.2287% | 22.2144% | 22.2144% | 22.2144% | 21.9933% |
| claude-8 | 2026-10-02T15:00:00Z | 153.80 | 34.0000% | 34.0000% | 34.0000% | 34.0000% | 34.0000% |
| claude-1 | 2026-10-03T06:00:00Z | 77.58 | 95.9459% | 95.9459% | 95.9459% | 95.9459% | 95.9459% |
| claude-9 | 2026-10-03T13:00:00Z | 88.21 | 0.0000% | 0.0000% | 0.0000% | 0.0000% | 0.0000% |
| claude-4 | 2026-10-03T14:00:00Z | 109.52 | 1.9618% | 1.9618% | 1.9618% | 0.0000% | 45.0280% |
| claude-6 | 2026-10-03T14:00:00Z | 113.46 | 0.0000% | 0.0000% | 0.0000% | 2.6250% | 5.3963% |
| claude-7 | 2026-10-03T14:00:00Z | 77.72 | 68.8722% | 68.8722% | 68.8722% | 69.4092% | 72.5384% |

Raw variant and reset evidence: [rank-earliest-reset-replay.json:121](2026-10-03-rank-earliest-reset-replay.json#L121). Per-job quantiles: [JSON:46](2026-10-03-rank-earliest-reset-replay.json#L46). Two full replay runs yielded identical numeric metrics; all five variants passed accounting consistency checks, three synthetic cost/reading/reserve sanity checks passed, and the script compiled.

The requested properties use real `evaluate` over generated lane sets for both providers, with 400 examples per parametrized property. [test_scheduler_weekly.py:208](../../tests/unit/test_scheduler_weekly.py#L208) generates normal eligible fleets and existing admission refusals (caps, floor, model reserve, ownership, enabled status, desktop, identity, config directory, closures and exclusions). The independent reference retains the prior admission predicates; reserves only change ordering.

| Law | Source | Result |
| --- | --- | --- |
| Earlier weekly reset never ranks worse in the same preceding band/reserve classes | `tests/unit/test_scheduler_weekly.py:97` | 800 examples passed |
| Equal resets prefer more weekly headroom | `:107` | 800 passed |
| Under either reserve cannot beat an otherwise-equal clear lane | `:122` | 800 passed |
| Desktop last; turn affinity first within desktop class; Claude stranded precedence unchanged | `:135`, `:148` | 1,200 passed |
| Total, unique and deterministic order across input permutations | `:255` | 800 passed |
| No previously eligible lane becomes ineligible | `:274` | 800 passed |
| Explicit load-band and measured precedence | `:163`, `:179` | 1,600 passed |

All **33 tests passed**, comprising **6,800 generated examples** and **16 deterministic edge cases** for binding scope, missing/renewed resets, default/custom strict reserve boundaries, and weekly-before-five-hour precedence. Final property slices: 6/229.65s, 5/172.08s, 2/38.08s, 4/254.94s, 16/16.25s. Each final slice stayed below ten minutes.

[weekly_rank_mutations.py:25](../../tools/weekly_rank_mutations.py#L25) killed **10/10** mutations by test assertion failures: latest-reset-first, less-weekly-headroom-first, ignored weekly reserve, ignored five-hour reserve, swapped reserve precedence, ignored band, ignored measured, ignored desktop, ignored affinity and ignored Claude stranded precedence. Collection errors and inapplicable edits never count as kills. Mutations compile changed comparators in memory; production files are untouched. They ran in two five-mutation batches under ten minutes.

The remainder of this section records **historical initial-implementation verification and delivery**, including its environment failures, earlier overlong exploratory runs, and original bundle. These are not the round-two verification results. Current foreground slice counts, baselines, mutations and bundle are in [2026-10-04-rank-review-fixes.md](2026-10-04-rank-review-fixes.md).

Initial verification used the locked pytest 9.1.1 / Hypothesis 6.168.1 dependencies with CPython 3.12.14, except already-started 3.14.7 slices noted below. Counts are per suite/command; follow-ups overlap earlier rows. Collecting that scope confirmed **693 unique tests: 686 passed, 5 environment/timing failures, 2 skipped**.

| Verification | Final result | Foreground slice time |
| --- | --- | --- |
| Scheduler, turns, policy, picker, capacity, gate core | 332 passed before the final unknown-model picker regression; follow-up covers that extra test | 116.35s |
| Final picker unit + native integration | 42 passed (39 unit + 3 native) | 7.09s |
| Weekly generated laws | 33 passed, 6,800 examples | Five slices, longest 254.94s |
| Admission uncapped suite | 27 passed, unchanged 4,250 configured examples | Seven slices, longest 138.32s |
| Scheduler/reference split suite | 3 passed, 4,500 configured examples | 473.65s / 319.88s / 36.31s |
| Route-check suite after the stale-reset horizon fix | All 24 unique tests passed, 5,000 generated cases across properties | Ordinary cases 18.64s; general property 596.22s; no-change/unrelated properties 384.02s; targeted-commit property three 500-example seeds at 83.91s / 80.40s / 80.42s |
| Busy usage reads | 98 passed (96 ordinary + two inherited properties, 120/60 examples) | 161.96s / 59.10s / 66.57s |
| Timer probe suite | 34 passed, 1 existing timing failure | 55.61s |
| Codex probe + Claude usage sensor suites | 83 passed | 3.95s |
| Fake picker, routing, admission priority, busy admission, probe recovery | 51 passed, 4 sandbox failures, 2 skipped | 119.12s |
| Ranking mutation checks | 10/10 killed by assertions | Two five-check batches, each below ten minutes |
| Replay sanity/accounting/determinism and compilation | Passed | Two foreground runs, three synthetic checks, five variant consistency checks |

The route targeted-commit property normally configures 1,500 examples. To keep that verification within the requested foreground slice limit, a temporary collection plugin under `/private/tmp` ran three 500-example slices with Hypothesis seeds 1/2/3. Tests and their assertions were not edited. The other route and scheduler/reference properties retained their configured example counts. The temporary runner's pytest import-rewrite warnings do not affect test assertions.

The four fake admission-priority failures are at [test_admission_priority.py:57](../../tests/fake/test_admission_priority.py#L57), `:71`, `:85`, and `:179`: three raise `PermissionError` because the sandbox forbids `/bin/ps`; the reused-PID case cannot validate its process and therefore cannot obtain the expected background hold. Representative direct-ps and reused-PID failures also reproduce with checkpoint `3e31ab7b`'s scheduler compiled in memory. These tests were not changed.

The remaining timer failure is [test_timers_probe.py:361](../../tests/unit/test_timers_probe.py#L361), requiring a whole probe cycle to finish within 0.4s. The final full 3.12 slice measured 1.027s, with standalone retries at 1.566s / 0.635s. Checkpoint Timers reproduced the same assertion failure on 3.14 at 1.950s (and a separate 0.2s tick assertion failed there); checkpoint 3.12 passed once, so this is reported as timing variability under host/sandbox load rather than deterministic 3.12 baseline reproduction. Timing assertions remain unchanged.

There were initial overly broad exploratory runs that exceeded the user's ten-minute limit: core/admission 30:07, scheduler/reference 25:42, route-check 28:26, weekly laws 11:12 and mixed probe 10:56. This was a workflow error; final verification was divided into the slices above. The initial route run imported the old horizon implementation and found two real differential failures; the stale-window horizon change and regression at [test_route_check.py:390](../../tests/unit/test_route_check.py#L390) address them, and final route properties passed. All started processes were allowed to finish because the sandbox also denied the required process inspection; nothing was killed by pattern or left running.

Shared Git metadata is not writable: `git add` failed creating its `index.lock` with `Operation not permitted`. Commits therefore use the preserved workspace-local `.job-git` directory on `feat/rank-earliest-reset`. The local Git directory, SQLite snapshot, dependency environments and logs are not committed.

The delivery bundle is [2026-10-03-rank-earliest-reset.bundle](2026-10-03-rank-earliest-reset.bundle), with prerequisite `3e31ab7bd5bf06efbd5aa61592a542e2c7bf4d3d`. Its named head is `refs/heads/feat/rank-earliest-reset` (the final report commit, following `5110e5d4`). `git bundle verify` checks the prerequisite and object pack; `git bundle list-heads` names its exact head, which is also reported in the final delivery message. No history was rewritten, no push occurred, no live state or daemon was changed, and no task process remains.
