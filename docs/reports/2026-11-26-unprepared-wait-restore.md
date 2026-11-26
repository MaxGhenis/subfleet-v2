# Unprepared capacity wait rollback (issue 339)

Started at 2026-11-26T16:16:36Z on detached HEAD `a4d21501e`.

## Cause and changes

At `a4d21501e`, `Daemon._capacity_wait` replaces the cached record and advances its recheck count before the unprepared-wait transaction starts (`subfleet/daemon.py`, lines 7682–7707 and 8463–8474). `Store.transaction` rolls back the job row and any audit insert on a body, audit, or COMMIT failure (`subfleet/store.py`, lines 515–565), leaving the cache advanced. `why` reads the cache at daemon lines 4735–4736, and the capacity demand reader uses it for jobs already waiting on capacity at lines 2885–2898. After a failed repeat look, the next look would use recheck 2 and a 4 s clock because the delay doubles with each recheck (`subfleet/scheduler.py`, lines 1492–1495). After a failed first look, `why` could report an uncommitted wait until the next pass. That retry still starts at recheck 0 and 1 s on the baseline because daemon lines 8373–8379 clear a stale record when the job row has no capacity wait.

- `subfleet/daemon.py` adds `_capacity_wait_rollback` immediately above `_isolated_route`. It holds the writer lock across the cache snapshot, wait update, and transaction exit, and restores the prior object or removes a newly created entry on failure. The unprepared route is evaluated before entering the wrapper. The diff of the helper and its trailing blank line against `d6aa5b652:subfleet/daemon.py` lines 9672–9690 printed nothing.
- `tests/fake/test_unprepared_wait_rollback.py` injects a body exception, a SQLite audit trigger abort, and a denied SQLite COMMIT. It also exercises a successful control and counts helper definitions.
- `docs/acceptance-contract.md` adds the rollback guarantee to C-6.10 and the first version 3 change-log entry. The existing C-6.10 marker covers the change.
- This report records the regression, invariants, and verification.

## Invariants and coverage

1. **Restore on failure.** The repeat-look cases check object identity with `is` for all three faults. The first-look cases check that a previously absent record is removed for all three faults.
2. **Store and cache agree.** Both sets of fault cases compare the whole job row and the job's `job.unprepared_wait` events. The repeat-look cases compare all four recheck fields from the last committed look, and the first-look cases expect `None`. The initial dispatch confirmed `response["job"]["recheck"]`.
3. **Successful looks preserve baseline behavior.** The control passes on both versions. It checks the initial unprepared signature, recheck 0, a 1 s clock, one event and zero attempts, then recheck 1, a 2 s clock and one additional event. The fault cases check the same clock and event progression when retried successfully. The requested module run covers the surrounding capacity, worktree, task-hold and routing behavior.
4. **One helper.** `test_one_capacity_wait_rollback_helper` counts the definition and requires exactly one.

## Fail first

The failing assertions on `a4d21501e` were as follows. The repeat-look failures occurred after the full-row and event comparisons passed.

| Cases | Failing output, abbreviated |
| --- | --- |
| `test_failed_repeat_look_keeps_recorded_wait[body]`, `[audit]`, `[commit]` | `assert service._capacity_waits[job] is previous` — a new record with `rechecks: 1` replaced the prior record with `rechecks: 0`. |
| `test_failed_first_look_leaves_no_wait[body]`, `[audit]`, `[commit]` | `assert job not in service._capacity_waits` — the cache still contained the job with `rechecks: 0`. |
| `test_one_capacity_wait_rollback_helper` | `assert 0 == 1`. |

Pytest reported `7 failed, 1 passed in 30.32s`. The passing case was `test_failed_repeat_look_keeps_recorded_wait[control]`. With the fix, pytest reported `8 passed in 15.79s`.

## Mutants

| Mutant | Cases that failed |
| --- | --- |
| M1, no wrapper | Repeat and first looks, all three faults. |
| M2, wrapper ends before the transaction | Repeat and first looks, all three faults. |
| M3, restore only when there was a prior record | First look, all three faults. |
| M4, catch only `sqlite3.Error` | Repeat and first looks, body fault only. |
| M5, duplicate helper | One-helper check only. |

## Test runs

Each run used the following command with `job="$PWD/build/uwr-build-r1"`. For `baseline`, `fixed`, and `m1` through `m5`, the only module argument was `tests/fake/test_unprepared_wait_rollback.py`. The `modules` run used the module list below once.

```sh
uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest \
  -n 4 --timeout=540 --basetemp "$job/pytest-$run" -q "$@"
```

```text
tests/fake/test_unprepared_wait_rollback.py
tests/fake/test_idle_capacity_events.py
tests/fake/test_idle_capacity_refresh_bound.py
tests/fake/test_capacity_wait_backoff.py
tests/fake/test_unrun_worktrees.py
tests/fake/test_task_hold_clocks.py
tests/fake/test_idle_route_deferrals.py
tests/fake/test_admission_route_isolation.py
tests/fake/test_state_contract.py
tests/unit/test_invariants_index.py
tests/unit/test_test_module_names.py
tests/unit/test_handler_refusals.py
```

Free space comes from `df -H /`. Local time and load averages come from `uptime`. Basetemp sizes are from `du -sk` before guarded deletion.

| Run | Passed | Failed | Skipped | Basetemp (KiB) | Free | Local time | Load averages |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| baseline | 1 | 7 | 0 | 4620 | 76 GB | 17:20 | 38.41 51.74 60.35 |
| fixed | 8 | 0 | 0 | 4620 | 76 GB | 17:22 | 48.49 49.80 58.32 |
| m1 | 2 | 6 | 0 | 4608 | 76 GB | 17:23 | 62.31 53.71 59.08 |
| m2 | 2 | 6 | 0 | 4588 | 76 GB | 17:24 | 38.17 48.53 56.89 |
| m3 | 5 | 3 | 0 | 4616 | 76 GB | 17:25 | 43.56 47.05 55.65 |
| m4 | 6 | 2 | 0 | 4620 | 76 GB | 17:26 | 62.14 51.65 56.86 |
| m5 | 7 | 1 | 0 | 4620 | 76 GB | 17:26 | 50.03 50.11 56.11 |
| modules | 798 | 1 | 0 | 172048 | 76 GB | 17:27 | 39.65 47.42 54.88 |

The module run failed in `tests/fake/test_unrun_worktrees.py::test_a_task_hold_that_commits_while_the_look_is_under_way_takes_the_job` with `threading.BrokenBarrierError`. The test pauses admission at `_capacity_wait` (line 577) and waits for a task-hold write before releasing the admission thread. The required wrapper now holds the writer lock at that pause. The lock warning showed the task-hold write waiting, and the test's 15 s barrier expired. That test file is outside the authorized edit scope. All eight new cases passed in this run.

Hub note (2026-11-26): the hub's commit d51620224 moves that test's stop from `_capacity_wait` to `_capacity_wait_rollback`, which the pass enters before it takes the writer lock, so the hold commits at the same point as before: ahead of the wait. The hub's run of the same 12 modules passed 799 of 799. With the hold read removed from the unprepared write, the moved test fails.

## Scope and workspace

The reserving, probe-wait, and fence-wait transactions on `release/217` lack cache restore until D-RESERVED-NOISE1 lands. The helper retains the original docstring reference to C-3.8. That clause is absent from `a4d21501e` and comes from D-RESERVED-NOISE1. The hub owns the tree-thrash test and full CI suite.

The sandbox denied the requested Darwin temp directory and write access to the existing uv cache. The tests used `build/uwr-build-r1` with a private copy of the cache. The checkout had no venv, so after the import check failed, `uv sync --group dev --offline` installed the dev dependencies there. The pytest failed-state hook was redirected into the job folder to keep it away from shared test artifacts.

Git staging was denied by `index.lock` permissions in the worktree's shared Git metadata. The four intended commits are represented by worktree changes for salvage. The commit list from `git log --oneline a4d21501e..HEAD` is empty. Mutations ran against the verified fixed worktree file. After each, the byte-for-byte backup was restored and both the file and its patch against HEAD matched with `git diff --quiet --no-index`.

Each basetemp stayed below 200 MB. The combined scratch footprint reached at least 241,064 KiB during the module run, exceeding the 200 MB limit. The 172,048 KiB basetemp shared the job folder with the private uv cache and test environment. The basetemps, job folder, and generated pytest and Hypothesis caches were deleted. The job left no files outside the worktree.
