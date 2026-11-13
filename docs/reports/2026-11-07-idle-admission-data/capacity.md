# Capacity change findings

Change 2 compares each admission pool's active closure conditions, lane enrollment/enabled state and policy digest with its previous pass. Disappearing closure conditions, lane changes and policy changes bring the same eligible capacity waits forward as a released lease. A probe's own inconclusive wait (`expedite=False`), workspace/route waits and the existing exclusions keep their clocks. The lane fingerprint also includes credential epoch/reference, ownership and desktop status. A committed closure/lane revision, captured before the census, covers conditions inserted after the census and observed by a later route but gone before the next census. This uses the committed mutation observer plumbing shared with Change 1, without relying on its rest deadlines. A per-pool pending retry flag also retains capacity news after a failed partial pass until a whole pass succeeds; otherwise an early store error could consume a release before the remaining 600 s waits were reached.

The ceiling is 600 s only when every rejection of every evaluated lane is event-only. Empty evaluations, candidates, missing reasons, mixed reasons and unknown reasons retain 30 s. Duplicate closure renderings must all identify operator holds: a provider limit sharing a hold's scope/deadline cannot borrow its longer clock. A due unprepared job now evaluates its full route without preparing a workspace or writing a decision, because C-6.20's standing-refusal list omits dynamic reasons. Both waiting paths use `waiting_metadata`, retaining earlier closure and reading reset clocks.

| Scheduler reason or category | Ceiling | Why |
| --- | ---: | --- |
| `disabled` | 600 s | Observed lane state change ends it. |
| `needs a shell: MODEL runs SANDBOX jobs without one` | 600 s | Enrollment/policy capability changes end it. |
| `closed:SCOPE:UNTIL`, all matching causes `operator-hold` | 600 s | Observed release/expiry ends it; known reset still clips the clock. |
| `closed:SCOPE:UNTIL`, `provider-limit` | 30 s | A provider reading can change availability. |
| `closed:SCOPE:UNTIL`, `credits` | 30 s | A reading or time can change availability. |
| `closed:SCOPE:UNTIL`, `auth-dead` | 30 s | No proof every recovery reaches an observed change. |
| `closed:SCOPE:UNTIL`, `cooldown` | 30 s | Time can end it. |
| Unmatched closure rendering | 30 s | Unknown cause is conservative. |
| `excluded` | 30 s | Conservative; attempt/retry routing can change demand. |
| `desktop` | 30 s | Desktop registry/login reading can change. |
| `config-dir` | 30 s | Conservative. |
| `owner-v1` | 30 s | Conservative. |
| `identity-mismatch` | 30 s | Conservative. |
| `credential-refused` | 30 s | Conservative. |
| `no-slot` | 30 s | Slots, fleet/parent/provider caps and unavailable-lane guards can change. |
| `below-floor` | 30 s | Headroom readings can change. |
| `reserve:MODEL:unmeasured` | 30 s | Reading can change. |
| `reserve:MODEL:reserved` | 30 s | Reading/reset can change. |
| `reserve:MODEL:probe-required` | 30 s | Probe evidence can change. |
| Derived `unknown`, `no-lanes`, `credential-latched`, `auth-dead` | 30 s | Conservative; these are standing-refusal/summary labels rather than additional `judge_lane` append sites. |
| Summary/hold labels `provider-cap`, `fleet-full`, `parent-cap`, `slot-kept`, `provider-slot-kept` | 30 s | Caps/leases can change; chosen-lane waits never get the event-only ceiling. |
| Any future reason, missing/nonstring reason | 30 s | Total default is conservative. |

`_reset_demand` has no `checked_at` freshness predicate. It uses `lane_wait`, the cached wait label and the latest per-pass hold to qualify jobs, then evaluates routing against the current store. Changing cached look age therefore leaves both demand qualification and current routing answers unchanged. `why` exposes the truthful latest `checked_at`/next clock; its timestamp can be older, as intended, but it has no freshness refusal. `daemon.status` and C-6.11's idle line derive their clock and reasons from admission's separately refreshed aggregate state.

The not-due path still runs task/pin and door guards, derives retry/model/lane demand, preserves FIFO waiters, probe ordering and lease queues, recomputes applicable standing refusals, refreshes `behind` and reports the cached hold with its clock. The whole pass still offers C-6.20's unrun give-back sweep. It skips workspace preparation, full route evaluation and capacity-wait writes for that job.

Invariant tests in `tests/fake/test_idle_capacity_events.py`:

| Invariant | Test |
| --- | --- |
| 4, every eligible backed-off wait comes forward | `test_c6_10_capacity_events_bring_every_eligible_wait_forward` (released, expired, enabled, disabled, enrolled, removed, policy; three ordinary waits plus one probe-own exception) |
| 5, every defined reason plus unknown classified | `test_c6_10_every_rejection_reason_has_a_conservative_classification`, `test_c6_10_only_an_operator_closure_is_event_only`, `test_c6_10_every_lane_and_every_reason_must_be_event_only`, `test_c6_10_a_provider_limit_cannot_borrow_an_operator_holds_deadline` |
| 6, six commits per hour per waiting job at ceiling | `test_c6_10_long_wait_commits_at_most_six_times_per_hour_per_job` (four jobs, 24 commits over 3,600 fake seconds) |
| 7, old/new release differential | `test_c6_10_released_hold_places_the_old_rules_jobs_in_order_no_later` (release at t=17 s: new starts t=17, old 30 s rule starts t=30; all four jobs in identical order) |
| 8, reset demand unchanged between looks | `test_c23_16_demand_is_identical_between_old_and_long_capacity_looks` (ages 0, 29, 31, 299, 599 s) |
| Failed partial pass retains release news | `test_c6_10_a_failed_pass_keeps_capacity_news_for_every_wait_on_retry` (three 600 s waits; first reserved, second raises a SQLite error; retry reserves both remaining jobs) |
| Changes observed only by a route between censuses | `test_c6_10_a_condition_seen_only_after_the_census_still_brings_waits_forward` (closure added then released; lane disabled then enabled) |
| Known reset and default 30 s | `test_c6_10_known_operator_reset_still_beats_the_long_clock` (prepared/unprepared, reset at 123 s), `test_c6_10_reading_or_time_verdicts_keep_the_30_second_ceiling` |
| No unconditional bring-forward | `test_c6_10_no_change_never_brings_waits_forward` |

Invariant 6 needs a qualification: preserving C-6.10's requested 1 s, 2 s, 4 s exponential ramp necessarily exceeds six commits in the first hour. The bound is steady state after reaching 600 s; the test explicitly establishes that ceiling before measuring. The first 3,600 s from a fresh unchanged verdict have 15 looks including the initial one (t=0,1,3,7,15,31,63,127,255,511,1023,1623,2223,2823,3423). The implementation preserves the requested ramp and documents the transient deviation from an unconditional interpretation.

Commands/results specific to Change 2:

- Default `UV_CACHE_DIR` at `$(getconf DARWIN_USER_TEMP_DIR)/uv-cache` failed before pytest with an Operation not permitted opening `sdists-v9/.git`; permitted `/private/tmp` child caches worked.
- `uv --cache-dir /private/tmp/idle-admission-uv-cache run --no-sync pytest tests/fake/test_idle_capacity_events.py -q -k 'classification or every_lane or only_an_operator' --basetemp /private/tmp/idle-capacity-pure-2`: 30 passed, 21 deselected, 1.09 s.
- Updated pure subset with duplicate-cause guard: 31 passed, 22 deselected, 7.39 s (`/private/tmp/idle-capacity-pure-4`, cache `/private/tmp/idle-admission-build/uv-cache`).
- `uv ... run --no-sync pytest tests/fake/test_idle_capacity_events.py tests/fake/test_capacity_wait_backoff.py -q --basetemp /private/tmp/idle-capacity-integrated-1`: 81 passed, 2 fixture failures, 42.77 s. Removal was initially attempted on a referenced lane; the corrected fixture removes an unused enrolled lane. The unprepared reset fixture needed a committed Git repository; it now creates its own empty baseline.
- `uv ... run --no-sync pytest tests/fake/test_idle_capacity_events.py -q --basetemp /private/tmp/idle-capacity-integrated-2`: all 53 passed, 37.09 s. All 30 pre-existing capacity-backoff tests passed in the preceding combined run.
- After the committed-revision race fix: `uv ... run --no-sync pytest tests/fake/test_idle_capacity_events.py -q --basetemp /private/tmp/idle-capacity-race-1`: all 55 passed, 52.85 s.
- After retaining capacity news across failed partial passes: `uv ... run --no-sync pytest tests/fake/test_idle_capacity_events.py -q --basetemp /private/tmp/idle-capacity-failure-news-1`: all 56 passed, 11.77 s.
- All test commands set `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"` and the named `/private/tmp` UV cache. `git diff --check` passed. No new event/record key or refusal was introduced.

Review overlap: the due unprepared fast path is before #295's described line boundary; the ordinary waiting metadata call lies later in `_admit_pass`, so a nearby-hunk merge may need attention. There are no edits to `_batches`, `_record_owned` or the unrun sweep.

Independent review found and fixed a census race: a closure inserted after the initial census and gone/expired before the next census can be observed by a job's route but absent from both census sets. Committed closure/lane revisions now cover this gap. Two regressions exercise a closure appearing only after the census then released before the next pass, and the equivalent lane disabled/enabled sequence. Root coordinates the final required-suite and mutant reruns against this fix.
