# Liveness pacing follow-up for issue #323

Started at `2026-11-26T16:16:44Z`. Verified `git rev-parse --short=9 HEAD` returned `a4d21501e`.

## Changes and invariants

`tests/unit/test_liveness_write_pacing.py` covers the three review items. Source line numbers refer to `subfleet/daemon.py` at `a4d21501e`.

1. **After a committed observation, the pace is its monotonic sampling time.** Case A, `test_folded_identity_write_paces_from_its_observation_not_its_commit[owned]` and `[counted]`, checks that identity events carry the observation (11233–11251) and publish the pace after commit (11261–11262) using the sample from line 11216. Both cases sample at 60, commit at 70, and write the next standalone observation at 120. The saved age after each inspection peaks at 59 seconds. `test_liveness_pace_starts_at_observation_before_a_slow_successful_commit` already covers the standalone path (11264–11278).
2. **Ending drops the attempt's pace entry immediately and preserves other attempts' entries.** Case B, `test_unlaunched_and_resolved_attempts_drop_their_liveness_pacing_entry[unlaunched]` and `[quarantine-resolved]`, checks the pops in `_unlaunched` (11336) and `_resolve_quarantine` (11810). Both cases seed a second attempt's pace and check its value after the method returns. `test_ended_attempts_leave_no_liveness_pacing_entry` covers success and loss through `_finalize` (12530), quarantine (11767), and `_forget_paced` (5787–5811). `test_terminal_attempt_worker_also_drops_liveness_pacing_entry` covers `_process_attempt` (10443).
3. **A retry saves its own wall timestamp and monotonic pace.** Case C renames the test to `test_failed_liveness_commit_retries_the_same_table_with_a_new_timestamp`. It refuses the write at 60, then retries the same table at 60.9. It checks that the failed write leaves evidence, pace, and the recorded table alone and that the retry saves its new wall timestamp and pace. This pins the sampling at 11213 and 11216, standalone pace publication at 11278, and table history advancement at 11281.

`docs/acceptance-contract.md` replaces one C-5.12 sentence and adds the first version 3 change-log entry, dated from the UTC start. This report records the changes and verification. The daemon source, invariants index, and November 6 build records are unchanged.

## C-5.12 sentence

**Before**

> A failed write retries its observation, restart forgets the pace so its first observation writes, and attempt end drops the entry.

**After**

> A failed write leaves its table unrecorded, so its retry, on that table or a newer one, saves a new timestamp taken when the retry runs; restart forgets the pace so its first observation writes, and attempt end drops the entry.

## Mutation results

Each mutant ran alone before the clean run. The following are the failure IDs and assertion excerpts. All other cases in each mutant run passed.

**M1** used the commit-time `time.monotonic()` at line 11262. Both cases failed at the same assertion.

```text
FAILED tests/unit/test_liveness_write_pacing.py::test_folded_identity_write_paces_from_its_observation_not_its_commit[owned]
FAILED tests/unit/test_liveness_write_pacing.py::test_folded_identity_write_paces_from_its_observation_not_its_commit[counted]
>       assert daemon._last_observed_alive_write[ATTEMPT] == 60
E       assert 70 == 60
tests/unit/test_liveness_write_pacing.py:381: AssertionError
```

**M2** removed the pop at line 11336.

```text
FAILED tests/unit/test_liveness_write_pacing.py::test_unlaunched_and_resolved_attempts_drop_their_liveness_pacing_entry[unlaunched]
>       assert ATTEMPT not in daemon._last_observed_alive_write
E       AssertionError: assert '20260905-100000-settle/a1' not in {'20260905-100000-settle/a1': 0.0, 'still-live/a1': 1.0}
tests/unit/test_liveness_write_pacing.py:494: AssertionError
```

**M3** removed the pop at line 11810.

```text
FAILED tests/unit/test_liveness_write_pacing.py::test_unlaunched_and_resolved_attempts_drop_their_liveness_pacing_entry[quarantine-resolved]
>       assert ATTEMPT not in daemon._last_observed_alive_write
E       AssertionError: assert '20260905-100000-settle/a1' not in {'20260905-100000-settle/a1': 0.0, 'still-live/a1': 1.0}
tests/unit/test_liveness_write_pacing.py:494: AssertionError
```

## Test commands and counts

`job_dir` was `$PWD/build/pct-build-r1`. The first command ran once each with `run=m1`, `m2`, `m3`, and `base`. The `base` run used the original daemon source at `a4d21501e` and the updated tests.

```sh
uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest \
  -n 4 --timeout=540 --basetemp "$job_dir/pytest-$run" -q \
  tests/unit/test_liveness_write_pacing.py

uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest \
  -n 4 --timeout=540 --basetemp "$job_dir/pytest-final" -q \
  tests/unit/test_liveness_write_pacing.py tests/unit/test_daemon_settle.py \
  tests/unit/test_census_merge_liveness.py tests/unit/test_census_round2.py \
  tests/unit/test_census_round3.py tests/fake/test_state_contract.py \
  tests/unit/test_invariants_index.py tests/unit/test_test_module_names.py \
  tests/unit/test_handler_refusals.py
```

| Run | Passed | Failed | Skipped | Elapsed | `du -sk` basetemp | `/` free | `uptime` local time and load averages |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `m1` | 23 | 2 | 0 | 28.83 s | 5,880 KiB | 76 GB | 17:21, 36.32 / 48.89 / 58.83 |
| `m2` | 24 | 1 | 0 | 27.31 s | 5,920 KiB | 76 GB | 17:21, 43.42 / 49.04 / 58.42 |
| `m3` | 24 | 1 | 0 | 22.66 s | 5,880 KiB | 76 GB | 17:22, 48.71 / 49.86 / 58.39 |
| `base` | 25 | 0 | 0 | 28.32 s | 5,960 KiB | 76 GB | 17:22, 51.94 / 50.49 / 58.32 |
| `final` | 584 | 0 | 0 | 163.40 s | 51,980 KiB | 76 GB | 17:26, 62.14 / 51.65 / 56.86 |

Disk and load readings came from `df -H /` and `uptime` before each run. Each basetemp was measured and removed before the next run. All basetemps were below 200 MB.

## Limits and deviations

- `_record_owned` samples time when it runs. A saved observation can therefore be later than the table read by up to the table's age, as it could before pacing.
- The supplied checkout was detached. The sandbox denied `index.lock` creation for staging and commits. `git log --oneline a4d21501e..HEAD` is empty. The three deliverables are in the worktree for salvage. The sandbox also denied `git restore --source=HEAD -- subfleet/daemon.py` after each mutant. I restored the saved source bytes and checked `git diff --quiet -- subfleet/daemon.py` each time.
- The sandbox denied creation of the job folder in the Darwin temp directory and write access to its existing uv cache. I used the specified workspace fallback and copied that cache there. The supplied checkout lacked a virtual environment. The import preflight failed, so I created a local environment with `uv sync --group dev --offline`. All test runs used offline uv and four workers.
- I removed the workspace job directory and pytest cache after the runs. No job files or directories were left outside the worktree. The pre-existing Darwin uv cache remains.
