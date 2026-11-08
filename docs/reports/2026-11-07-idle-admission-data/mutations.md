# Mutation verification

The runner copied the production package, tests and pyproject into isolated temporary directories. It changed only those copies, ran each selected regression with its own basetemp, and removed the copies after the run. The checkout never contained a mutant. JUnit identified actual test failures; fixture and collection errors do not count as kills.

Final baseline: 79 passed in 24.654 seconds. This snapshot includes closure/lane revision tracking, both expiry-during-pass regressions, the malformed-closure expiry guard, and capacity news retained across a failed pass. All recorded source and new-test SHA256 hashes match the current checkout.

| Mutant | Killing test | Seconds |
| --- | --- | ---: |
| no-rest | `test_idle_pass_rests_from_its_end_for_at_least_four_durations[0.01]` | 7.575 |
| no-submit-wake | `test_each_semantic_wake_starts_detached_on_the_next_tick[submit]` | 9.136 |
| no-released-closure-wake | `test_each_semantic_wake_starts_detached_on_the_next_tick[closure-release]` | 9.403 |
| 600-seconds-for-every-verdict | `test_c6_10_reading_or_time_verdicts_keep_the_30_second_ceiling[provider-limit]` | 6.989 |
| no-released-closure-bring-forward | `test_c6_10_capacity_events_bring_every_eligible_wait_forward[released]` | 13.069 |
| bring-forward-every-pass | `test_c6_10_no_change_never_brings_waits_forward` | 12.775 |
| unknown-reason-event-only | `test_c6_10_every_rejection_reason_has_a_conservative_classification[future-rejection-False]` | 12.690 |

Result: 7/7 killed; each pytest invocation ran one selected failing test and exited 1 without errors. The runner exited 0.

The no-closure-bring-forward mutant removes both the closure-set difference and the revision fallback; either remaining mechanism would correctly bring the released-closure wait forward. The failed-pass retry flag remains, and is unset in that normal release scenario. The initial 74-test baseline/first seven kills are preserved under `mutations-initial/`; the 78-test baseline/seven kills before failed-pass news retention are under `mutations-pre-retry/`.

Command (run three times as review found and fixed the expiry, short-lived-change and failed-pass edge cases):

```sh
TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR=/private/tmp/idle-admission-uv-cache uv --cache-dir /private/tmp/idle-admission-uv-cache run --no-sync python tools/idle_admission_mutations.py
```

The default uv cache was denied by the sandbox during the earlier store test invocation, so the permitted cache child above was used. The runner gives its test subprocesses separate permitted temporary children for isolation. Exact commands, source/mutant hashes, counts, log paths and failures are in `mutations.json` and `mutation-*.log`.
