# D-QOS-BOOKED1 (#277): a per-job provider QoS for priority callers

The build (`e4b68e3e0`, over release/217 `676a34bf1`) is described in the PR body. Review r1
(`docs/reports/2026-10-28-pr277-review-r1b.txt`) asked for one change, P2, and completed no
pytest run. This report covers fix round b2.

## Fix round b2

Fix commit `645af257a`, over `faeb79b57` (the build plus review r1's two regression files and
its report). All times are UTC on 2026-10-28. Loads are the 1-minute average at the hold's
start and end; "admitted" is the 1-, 5- and 15-minute load when the shared suite lock admitted
the hold. Every run was under the lock, one pytest per hold, with no xdist (the locked dev group
has none), a private `TMPDIR` and a private Hypothesis store.

### Decision 1: every job reader gives `provider_qos` as `utility`

`store.job_flags` now makes every job-domain record. It is `shell_flags` plus the request:
NULL, which every job from before the column has, and a missing column, which a read-only
`Store` sees on a schema-9 file it may not migrate, both read as `utility`. `shell_flags` is
back to the boolean launch fields alone, so attempt rows gain no job field. Raw SQL still
reads NULL.

The readers that return a job row, each now through `job_flags`:

| Reader | Where | Reached by |
|---|---|---|
| `Store.get_job` | `subfleet/store.py:819` | the daemon's `_job` (`show`, `wait`, `why`, kill and the rest), the conversation service |
| `Store.get_job_by_request` | `subfleet/store.py:823` | request-id lookups |
| `Store.list_jobs` | `subfleet/store.py:835` | capacity, retention, retention archive, retention survey (a read-only Store) |
| The daemon's `list` op | `subfleet/daemon.py:3656` | `subfleet runs` |
| The daemon's status-view jobs (`STATUS_JOBS`) | `subfleet/daemon.py:1611` | status and the operator's views |
| `Offline._job_dict` | `subfleet/offline.py:321` | offline `show_job`, `list_jobs` and status's running and turn lists |

The build already gave `utility` for NULL in the first five readers, and for a missing column
in the sixth (offline used `setdefault`). The missing column was absent from the first three.
The daemon's readers always read its own writable store, which is migrated when it opens, so
they see NULL and never a missing column. Every other read of `jobs` selects named columns or
is raw SQL used internally. The scheduler reads the request through `requested_qos`, which
gives `utility` for NULL and for a missing key alike.

New tests, beside review r1's red one:
- `test_store_qos_migration.py`:
  - `job_flags` reads NULL and a missing column as `utility`, and `shell_flags` adds no job field;
  - attempt rows gain no job field, over a migrated store and a read-only schema-9 one, offline included.
- `test_launch_provider_qos.py::test_d_qos_booked1_runs_show_carries_the_request`: the daemon's `list` op reads `default` and `utility`, and raw SQL keeps NULL.

### Decision 2: invariant 5

It holds as written for every job reader in the table above. The PR body's sentence should say
that raw SQL keeps NULL, as the build's own text already did; the lines are below.

### Red and green

| Run | Revision | Start | Load | Result |
|---|---|---|---|---|
| Red | `faeb79b57` | 18:04:37 | 78 → 60 | `test_qos_review_r1.py`: 3 failed (`KeyError: 'provider_qos'` from `get_job`, `get_job_by_request`, `list_jobs` on a read-only schema-9 store), 6 passed |
| Green | `645af257a` | 18:20:09 | 62 → 43 | `test_qos_review_r1.py`: 9 passed; `test_store_qos_migration.py`: 13 passed |

### Holds at `645af257a`

| Hold | Start | Load | Admitted | File: result |
|---|---|---|---|---|
| Green | 18:20:09 | 62 → 43 | 18:22:10 | `tests/unit/test_qos_review_r1.py`: 9 passed; `tests/unit/test_store_qos_migration.py`: 13 passed |
| PR fake files | 18:36:59 | 69 → 33 | 18:39:31 | `tests/fake/test_launch_provider_qos.py`: 21 passed; `tests/fake/test_launch_qos_review_r1.py`: 6 passed; `tests/fake/test_provider_qos_booked.py`: 1 passed, 1 skipped |
| Property | 18:43:05 | 26 → 68 | 18:45:12 | `tests/unit/test_launch_qos_property.py`: 10 passed |
| PR unit and guardian files | 18:46:26 | 67 → 58 | 18:47:30 | `tests/unit/test_guardian_qos_request.py`: 19; `test_cli_qos.py`: 12; `test_guardian.py`: 13; `test_guardian_qos.py`: 14; `test_store_migration_2.py`: 21; all passed |
| Guards | 18:48:58 | 56 → 59 | 18:48:59 | `tests/fake/test_state_contract.py`: 37; `tests/unit/test_invariants_index.py`: 25; `tests/unit/test_test_module_names.py`: 1; all passed |
| State-root tail | 18:51:02 | 65 → 59 | 18:51:02 | `tests/unit/test_live_store_guard_state_root.py`: 22 passed (61 s, so it needed no split) |
| st3 part 1 | 18:52:45 | 65 → 67 | 18:52:46 | `tests/unit/test_live_store_guard_st3_mutations.py -k` 8 mutants: 8 passed |
| st3 part 2 | 18:58:26 | 54 → 71 | 18:59:41 | 6 passed |
| st3 part 3 | 19:04:48 | 70 → 64 | 19:04:49 | 6 passed |
| st3 part 4 | 19:07:22 | 75 → 100 | 19:07:23 | 5 passed; all 25 of the file passed |

- **The PR's test files.** `git diff --name-only 676a34bf1 HEAD -- tests` lists nine, and every one ran: the seven above plus `tests/unit/test_guardian.py` and `tests/unit/test_qos_review_r1.py`. `tests/unit/test_guardian*.py` is three files, and `tests/unit/test_store_migration*.py` is one.
- **The skip.** It is the 31-vs-20 priority test in `test_provider_qos_booked.py`: this lane is clamped (this job's own shell reads PRI 20 with `ps -o pri=`), so the test skips, and CI runs it.
- **The property.** It drew 600 examples: 188 inherit and 412 utility. Of the 188, 81 were honoured requests and 107 inherited from the operator env alone. The build observed 175, 425, 80 and 95 with another seed. The test asserts that both sides are drawn.

### Timing pairs: `tests/fake/test_daemon_contract.py`, head `645af257a` and base `676a34bf1`

The base was a scratch `git worktree add --detach` with its own venv, so it imported its own
`subfleet` (checked); it was removed afterwards. The test file, the fake harness's conftest and
`run_daemon.py` are identical at both revisions. Runs were interleaved. After the first base
run outlasted the tool's window, the second pair ran in two halves, the same halves at both
revisions (A: the first 11 tests in file order, B: the other 11).

| Run | Start | Load | Admitted | Result |
|---|---|---|---|---|
| Head 1, whole file | 19:10:05 | 93 → 17 | 19:10:17 | 11 failed, 11 passed (341 s) |
| Base 1, whole file | 19:17:28 | 28 → 115 | 19:17:35 | stopped by the deadline at 583 s, with 20 of 22 finished: 14 failed (one also errored in teardown), 6 passed; the 21st was running. Deduced from the progress line `FF...FFF..FFFFFF.FFEF`, whose 14 `F` tests match the 14 kept failure roots by name; the failure reasons were lost with the stop |
| Head 2, half A | 19:29:58 | 119 → 53 | 19:32:28 | 9 failed, 2 passed (382 s) |
| Base 2, half A | 19:39:54 | 62 → 91 | 19:44:13 | 7 failed, 4 passed (287 s) |
| Head 2, half B | 19:50:04 | 113 → 49 | 19:50:16 | 10 failed, 1 passed (273 s) |
| Base 2, half B | 19:55:26 | 39 → 64 | 19:55:29 | 6 failed, 5 passed (218 s) |
| Base 3, half A | 21:02 to 21:41 | 112 to 141 | never | four tries: one admitted too late to run, three not admitted in their window |

- **Every failure at both revisions is a harness wait.** Head: 28 were `condition timed out after 5s` and 2 were `run_daemon ... timed out after 3 seconds`. Base 2: 12 and 1. No other failure appeared.
- **Base fails the same cases.** Every test that failed at head also failed at base, except three, and each of those failed at head by a 5 s wait on a job that was progressing:
  - `test_c5_2_job_survives_submitting_client_exit` and `test_c5_5_a_held_escaper_quarantines_and_force_release_records_override` each failed once at head, in half A at load 94–119, and passed in head run 1. Head's kept root for the first shows a guardian spawned about 3 s after the job's directory, and the 5 s wait ending before `start.json`.
  - `test_c5_2_provider_receipts_preserve_raw_return_codes[rc4-limit-with-clock-4]` failed in both head runs. It passed in base run 2, at load 39 → 64, and was mid-run when base run 1 was stopped. In head run 1 its job finished, with rc 4 kept, 6 s after it was created. In head run 2 its guardian took 4.8 s to write `start.json`.
- **The guardian's start time is unchanged.** Over every kept failure root, the delay from the guardian's process start to its `start.json` has a median of 3.8 s at head (24 receipts) and 3.7 s at base (20). `proc_start` has one-second resolution, so each delay is overstated by up to 1 s at both revisions alike.
- **The PR's launch path adds no wait for a job without `--qos default`.** `_launch_qos` reads no ancestry, `guardian_qos_args` returns nothing, and the guardian adds one `os.access` and one environment read. Every head receipt records `provider_qos: utility`.

**Verdict:** this PR doesn't change these cases' timing. Their failures are the known 3 s and
5 s harness waits under load, at both revisions alike.

### Mutants

Each mutant was an exact source replacement, asserted to occur once. It was applied in the tree
for its one hold only, then restored byte for byte, and `git diff --quiet` was checked after each.
Guardian and daemon subprocesses therefore saw the mutant too.

| Mutant | Source | Files run | Killed by (failures) |
|---|---|---|---|
| Class check dropped (`priority_class(...) == "priority"` → `True`) | `scheduler.launch_qos` | property, fake launch, review r1 fake | 10, among them review r1's `why_says_why[not-listed]` and `[no-list]`, the property, the ancestor walk, every-other-launch ×3 (not-listed, no-list, empty-list), per-attempt freshness, fake `why`, and review r1's `cycle-unlisted` |
| Request check dropped (`requested_qos(job) != "default"` → `False`) | `scheduler.launch_qos` | same | 8, among them review r1's `why_says_why[utility]`, plus `[null]` and `[no-column]`, the property, every-other-launch ×2 (asked utility, asked nothing), and review r1's gate rounds with NULL and `utility` |
| A probe inherits (the probe's guardian asks `guardian_qos_args(self._launch_qos(job)[0])`) | `Daemon._execute_probe` | fake launch | 1: `a_probe_for_an_honoured_job_stays_clamped` |
| Operator env ignored | `guardian.provider_qos` | guardian, review r1 unit, property | 5: `either_side_unclamps[utility-inherit-None]`, `start_json_records...[utility-inherit-inherit]`, two operator-inherit tests in `test_guardian_qos.py`, the property |
| Receipt records the request (`start["provider_qos"] = qos_request`) | `guardian.run_guardian` | same, plus booked fake | 2: `start_json_records...[utility-inherit-inherit]`, `a_host_without_taskpolicy_records_inherit` |
| Operator env case-folded and trimmed (`.strip().lower()`) | `guardian.provider_qos` | guardian, review r1 unit, property | 6: review r1's `operator_qos_requires_the_exact_inherit_value` ×4 (`Inherit`, ` inherit`, `inherit `, `INHERIT`), `either_side_unclamps[utility-INHERIT-utility]`, the property |
| A reader drops the missing-column default (`job_flags` keeps NULL → `utility` but not a missing key) | `store.job_flags` | review r1 unit, store migration | 7: review r1's three read-only readers, offline `schema9`, offline on an old store, `job_flags` on a missing column, read-only schema-9 attempts |

- **The first five** are the builder's five and review r1's first five. Each was killed by the test review r1 named, and by the builder's named tests that were in the files run.
- **The sixth** was a gap review r1 raised. Review r1's new test kills it, and so do two of the build's own tests.

| Mutant | Start | Load | Admitted | Result |
|---|---|---|---|---|
| Class check | 20:02:23 | 100 → 197 | 20:03:53 | 10 failed, 27 passed |
| Request check | 20:11:19 | 169 → 115 | 20:15:58 | 8 failed, 29 passed |
| Probe inherits | 20:29:47 | 79 → 62 | 20:30:40 | 1 failed, 20 passed; its first try, from 20:19:38, was not admitted |
| Operator env ignored | 20:41:43 | 157 → 100 | 20:44:16 | 5 failed, 60 passed; its first try, from 20:32:57, was admitted too late to run |
| Receipt records request | 20:47:28 | 97 → 155 | 20:47:47 | 2 failed, 64 passed, 1 skipped |
| Operator env normalised | 20:50:41 | 143 → 102 | 20:52:01 | 6 failed, 59 passed |
| Reader drops default | 20:53:45 | 95 → 104 | 21:00:04 | 7 failed, 15 passed |

### The process test

This job's own shell reads PRI 20 (`ps -o pri=`), so the lane is clamped. The 31-vs-20 test in
`tests/fake/test_provider_qos_booked.py` skipped, as on the build's lane, and is left to CI.
Nothing tried to leave the clamp.

### What didn't run

- **Base run 3** of `test_daemon_contract.py`: the lock admitted nothing in time during four tries (21:02–21:41, load 112–141), and the 4-hour mark followed. Base therefore has one complete run (in halves) and one stopped at 20 of 22 tests.
- **The 31-vs-20 priority test:** it skips on a clamped lane and runs in CI.
- **The full suite:** left to CI.
