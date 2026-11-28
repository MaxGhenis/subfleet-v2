# C-11.10 older CLI release hint (issue #327)

Started `2026-11-26T16:16:45Z`. Verified `git rev-parse --short=9 HEAD` returned `a4d21501e`.

The pre-#309 `why` hint `subfleet tasks release -- '?'` can release an existing task hold on `?`. The sandbox hold stays in force. The outcome otherwise depends on whether the policy routes `?`.

## Changes

- `docs/acceptance-contract.md` now states all three outcomes in C-11.10, corrects the 2026-11-14 change-log entry, and adds the 2026-11-26 entry for issue #327. All 257 contract markers match the base in order.
- `tests/unit/test_sandbox_hold_versions.py` adds cases (a)–(d), renames p7 to describe the fixture that refuses the hint, and shares the old CLI payload through `HINT`. The helper saves a real policy file containing `?`, validates it, and restarts the synthetic daemon on it.
- `docs/reports/2026-11-12-sandbox-hold.md` adds one correction inside the fix r1 p7 item. The raw `fix-r1-p7.json` still accurately records the old renderer output.
- `docs/reports/2026-11-26-release-hint-doc.md` records the correction and its evidence.

## Cause at `a4d21501e`

Policy validation accepts nonblank `chains` keys, so `?` loads and can use the wildcard sandbox permission. The daemon loads the policy at startup. See `subfleet/policy.py:365-366,531-539,564-566,48-52`, `subfleet/default_policy.json:64-67`, and `subfleet/daemon.py:1044-1047`.

The hint sends `{"action": "release", "task": "?", "until": null, "note": null}`. The old task wire shape is pinned at `tests/unit/test_sandbox_hold_versions.py:62-68`. The daemon maps this target to `task-hold:?`. An explicit sandbox target maps to the separate `sandbox-hold:workspace-write` key. See `subfleet/daemon.py:2050-2058` and `subfleet/task_holds.py:36-43,60-62`.

| Policy and store at release | Result | Base source |
| --- | --- | --- |
| No `?` key and no `task-hold:?` row | `unknown-task` (exit 2) before the write transaction | `subfleet/daemon.py:1640-1642,2032-2035,2055-2057,2135-2145`, `subfleet/contracts.py:213` |
| Policy has `?` and the row is absent | Success with `released: "?"`, `was_held: false`, `woken: 0`, and no writes | `subfleet/daemon.py:2147-2155`, `subfleet/store.py:536-546`, `subfleet/cli.py:2510-2512,2520-2527` |
| `task-hold:?` exists, including after a restart on a policy that dropped `?` | Deletes the task row, records `task.released`, and wakes eligible waiting jobs of `?` when no other hold covers them. `was_held` is true if the row was in force. | `subfleet/daemon.py:2055-2058,2144-2145,2160-2192,1922-1927`, `subfleet/store.py:536-546` |

## Invariants and witnesses

| Case | Test in `tests/unit/test_sandbox_hold_versions.py` |
| --- | --- |
| (a) | `test_documented_old_cli_hint_can_release_a_valid_question_mark_task_hold` |
| (b) | `test_the_older_cli_hint_releases_a_question_mark_row_the_policy_no_longer_routes` |
| (c) | `test_the_older_cli_hint_with_a_question_mark_task_and_no_row_writes_nothing` |
| p7 | `test_an_older_clis_release_hint_is_refused_without_a_question_mark_task` |
| (d) | `test_c_11_10_states_what_the_older_cli_hint_releases` |

1. The sandbox row is out of reach. Cases (a) and (b) compare `holder`, `acquired_at`, and `expires_at` before and after the release. Each finds exactly one new `task.released` event with `task: "?"` and no `sandbox` key. Case (a) also confirms the sandbox is the sole remaining hold. Case (c) and p7 compare store snapshots.
2. The hint refuses `unknown-task` (exit 2) exactly when both the policy key and row are absent. P7 and the repeated release in (b) check the refusal and unchanged store. Cases (a), (b), and (c) exercise admission when either is present.
3. Otherwise it releases task `?`. Case (c) checks the successful no-op response and zero SQLite changes. Cases (a) and (b) check task-row deletion and the task release event. Case (a) checks that the read-only Astra job moves from `task-held` to queued with its wait fields cleared, then gets one attempt. Case (b) checks the fallback after the policy drops the task, with zero jobs woken. `tests/fake/test_task_hold_admission.py::test_release_wakes_the_held_jobs_at_once` also checks waking multiple jobs.

The existing `store_image` helper snapshots row counts and key shapes because the store returns mappings and the helper takes `tuple(row)`. Cases (b) and (c) also compare SQLite's writer change counter on the no-write paths. P7 keeps its prescribed snapshot assertion.

## Fail-first and mutants

With only the test changes applied at `a4d21501e`, (a), (b), (c), and p7 passed by design. They exercise the behavior already in the daemon. Only (d) failed.

```text
>       assert "which the daemon refuses `unknown-task` (exit 2), releasing nothing;" not in clause
E       AssertionError: assert 'which the d...ing nothing;' not in '- **C-11.10...k_holds.py`.'
E         'which the daemon r... releasing nothing;' is contained here:
tests/unit/test_sandbox_hold_versions.py:218: AssertionError
FAILED tests/unit/test_sandbox_hold_versions.py::test_c_11_10_states_what_the_older_cli_hint_releases
1 failed, 34 passed in 29.81s
```

After the documentation edits, all 35 tests in the version module passed.

| Mutant | Cases that failed | What caught it |
| --- | --- | --- |
| M1, route `?` to the sandbox target | (a), (b), (c), p7 | The read-only job was placed during setup, the unknown-task hold was accepted, the no-row reply named the sandbox, and p7 received a successful release. |
| M2, exclude `?` from `_known_task` | (a), (b), (c) | The setup holds in (a) and (b) refused `unknown-task`. The no-row release in (c) also refused `unknown-task`. |
| M3, remove the row fallback | (b) | The release after restarting without `?` refused `unknown-task` despite the row. |

Every mutant was caught. After each run, `git restore --source=HEAD -- subfleet/daemon.py` was denied the index lock. I restored the saved original bytes and verified byte equality and `git diff --quiet -- subfleet/daemon.py` before continuing.

## Test runs

```sh
job_dir="$PWD/build/rhd-build-r1"
uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest -n 4 --timeout=540 --basetemp "$job_dir/pytest-<run>" -q <modules>
```

For `fail-first`, `after-change`, `m1`, `m2`, and `m3`, the sole module argument was `tests/unit/test_sandbox_hold_versions.py`. For `full`, the module arguments were

```text
tests/unit/test_sandbox_hold_versions.py
tests/unit/test_sandbox_hold_cli.py
tests/fake/test_sandbox_hold_release_rows.py
tests/fake/test_task_hold_admission.py
tests/fake/test_state_contract.py
tests/unit/test_invariants_index.py
tests/unit/test_desktop_ledger.py
tests/unit/test_test_module_names.py
tests/unit/test_handler_refusals.py
```

| Run | Start UTC | Free on `/` | `uptime` output | Pass | Fail | Skip | Basetemp KiB |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: |
| fail-first | 2026-11-26T16:21:23Z | 76G | `17:21  1 user, load averages: 39.49 48.76 58.55` | 34 | 1 | 0 | 0 |
| after-change | 2026-11-26T16:23:01Z | 76G | `17:23  1 user, load averages: 55.62 51.39 58.46` | 35 | 0 | 0 | 0 |
| m1 | 2026-11-26T16:24:06Z | 76G | `17:24  1 user, load averages: 41.93 49.59 57.35` | 31 | 4 | 0 | 0 |
| m2 | 2026-11-26T16:25:30Z | 76G | `17:25  1 user, load averages: 47.91 47.87 55.84` | 32 | 3 | 0 | 0 |
| m3 | 2026-11-26T16:26:46Z | 76G | `17:26  1 user, load averages: 50.03 50.11 56.11` | 34 | 1 | 0 | 0 |
| full | 2026-11-26T16:27:25Z | 76G | `17:27  1 user, load averages: 41.20 47.61 54.91` | 559 | 0 | 0 | 48912 |

Each basetemp was measured with `du -sk` after its run and removed after verifying its real parent and `pytest-` prefix.

## Limits and workspace

This build changes documentation and tests only. A pre-#309 CLI still shows the `?` hint. To release the sandbox hold with a policy that has a task named `?`, use a CLI with `--sandbox` and run `subfleet tasks release --sandbox workspace-write`. See `subfleet/task_holds.py:206-210` at the base.

The assigned checkout was detached at the expected commit. The named branch was checked out in the caller's workspace. The sandbox denied `index.lock` at each commit stage, so all four files remain in the assigned worktree for salvage. `git log --oneline a4d21501e..HEAD` is empty.

The requested Darwin temp folder and existing uv cache were unwritable. I used the allowed `build/rhd-build-r1` fallback and copied the existing cache into it. The import probe failed and this checkout had no `.venv`, so I ran `uv sync --group dev --offline`. Pytest and xdist resolved offline. I added the writer change-counter checks in (b) and (c) and the task audit check in (b) to directly verify the requested invariants.

The largest test basetemp was 48,912 KiB. The local job folder and venv used 83,220 KiB combined before cleanup. The existing shared uv cache was left in place. No `sf-th-*` directories remained under the Darwin temp root, the inherited job temp root, or `/tmp`. The workspace job folder and local venv were removed. No job-created folders were left outside the worktree.

## Exact edited contract text

### C-11.10, base line 609

Old

```text
its `why` says the operator holds `?` jobs and hints `subfleet tasks release -- '?'`, which the daemon refuses `unknown-task` (exit 2), releasing nothing; a sandbox hold is released with a CLI that has `--sandbox`.
```

New

```text
its `why` says the operator holds `?` jobs and hints `subfleet tasks release -- '?'`. The daemon reads that request as a release of task `?`, never of the sandbox hold, so it never changes `sandbox-hold:workspace-write`. Policy validation accepts any nonblank task name, `?` included. With no `?` key in the policy's `chains` and no `task-hold:?` row, the daemon refuses it `unknown-task` (exit 2), releasing nothing; with a `?` key and no row, it answers `was_held` false and writes nothing; while a `task-hold:?` row is there (a hold on a task `?`, or a row left from a policy that had one), it releases that hold as any task release would: it deletes the row, records `task.released` and wakes each job of task `?` that no other hold covers. A sandbox hold is released with a CLI that has `--sandbox`.
```

### 2026-11-14 change-log entry, base line 23

Old

```text
What a CLI older than the sandbox hold shows for one (`None until …`, and the `why` hint `subfleet tasks release -- '?'`, which releases nothing) is stated.
```

New

```text
What a CLI older than the sandbox hold shows for one (`None until …`, and the `why` hint `subfleet tasks release -- '?'`, which never reaches the sandbox hold but releases a hold on a task named `?`, a name policy validation accepts) is stated.
```

### 2026-11-26 change-log entry

Old entry absent

New

```text
- C-11.10 (2026-11-26, D-RO-ADMIT1, issue #327 from #309's review r2; `docs/reports/2026-11-26-release-hint-doc.md`): the `why` hint a CLI older than the sandbox hold shows, `subfleet tasks release -- '?'`, is a release of task `?` and never of the sandbox hold. Policy validation accepts a task named `?`, so the hint releases that task's hold while its `task-hold:?` row is there; with no such task and no such row it is refused `unknown-task` (exit 2). The clause and the 2026-11-14 entry said it released nothing. Text and tests only; no code changes.
```

The full lines before and after the edits are available with these read-only commands.

```sh
git show a4d21501e:docs/acceptance-contract.md | sed -n '23p;609p'
sed -n '9p;24p;610p' docs/acceptance-contract.md
```
