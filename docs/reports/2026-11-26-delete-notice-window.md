# Delete notice window and owed-step clock

Started at `2026-11-26T16:20:04Z`. The assigned worktree was detached at `a4d21501e`, which matched the requested base. The caller's worktree held `fix/delete-notice-window-217` at that commit.

## Causes

- **#328.** At `a4d21501e`, the hourly pass forwarded `notice_keep_days * 86400` in `subfleet/daemon.py:6096`. The delete turn omitted it at lines 6040–6043, and `DeletePath._pinned` omitted it and the path clock at `subfleet/retention_delete.py:1738–1739`. Consequently `_pin_reasons` used zero and the wall clock. Its cutoff and freshness checks at `subfleet/retention.py:261,285–287` defer to `subfleet/retention_notices.py:163–189`, where zero keeps every unread notice eligible to pin and a positive window expires a notice exactly at the cutoff.
- **#336.** `Daemon._retention` read the clock for its answer at `subfleet/daemon.py:6052`. The gate read it again at line 6034 after the readiness check, which can wait for the path lock in `subfleet/retention_delete.py:1214–1220`. A catch-up window could close during that wait. The gate would then admit the owed step while `_retention` still returned `None`. The scheduler treats that return as success and clears the failure count at `subfleet/daemon.py:5605–5609`. The catch-up had already cleared that count in the reported scenario.

## Changes

- `subfleet/retention_delete.py` adds a per-step `notice_keep_s` defaulting to zero and passes that keep and `self.now()` to the shared pins helper. Gate 8 and its two re-asks use the same check. The gate docstring records both keeps and the clock.
- `subfleet/daemon.py` gives the delete step the same merged notice policy as the hourly pass. After readiness, one clock reading decides whether a step can run and whether it is `"owed"` or `"free"`. `None` from the gate means there was no step. An owed step returns `DEFERRED` to the scheduler.
- `tests/unit/test_retention_delete_notice_window.py` covers notice age, cutoff, acknowledgement, dry-run, real daemon policy and both re-asks. `tests/unit/test_retention_delete_daemon.py` checks the forwarded keep and simulates the hourly deadline crossing during readiness, with and without debt.
- `docs/acceptance-contract.md` updates C-8.7 and its version 3 entries. `docs/desktop/retention-archive.md` adds Revision 9 and updates §18 on pins, readiness, settlement and journal recovery.
- `docs/reports/2026-11-18-retention-delete-step.md` corrects retry timing, journal recovery and job-result logging, readiness without a request, trailing whitespace and install provenance. Git confirms that `d6db9da85` has parent `4aed7a3c9`, 14 first-parent merges past `85769b826`.
- `docs/reports/2026-11-26-delete-notice-window.md` records this build and its verification.

## Invariants

1. **Same window as the pass.** Gate 8 and both re-asks delegate unread-notice pinning to `_pin_reasons` with the given keep and path clock, so their decision agrees for any age and keep. Cases (a)–(c) cover expired, fresh and cutoff notices. Case (e) covers dry-run. Case (g) adds fresh and expired notices immediately before each re-ask.
2. **Keep zero keeps for ever.** Case (d) checks explicit zero and the omitted keyword with a 2020 unread notice. The acknowledged case deletes the tree with zero keep.
3. **The daemon passes the policy.** Case (f) drives the real path for seven days, zero days and the seven-day default. The daemon lock and default-budget tests also check the seconds forwarded to `step`.
4. **One reading.** The owed-step test closes the catch-up window during `pending()` and expects `DEFERRED`, followed by a pass on the next turn. The mirror test expects the pass to go first when there is no debt. The open-window catch-up test expects `None` for a free step. The `LEAVE_DUE` cases test the owed return after every due-leaving pass.

## Fail first

Pytest against the original production code at `a4d21501e` reported:

```text
TypeError: DeletePath.step() got an unexpected keyword argument 'notice_keep_s'
AssertionError: assert True == 'free'
assert None is <object object at ...>
20 failed, 36 passed in 80.60s (0:01:20)
```

Eleven direct cases failed on the missing keyword. The seven-day and default real-daemon cases kept the 2020 notice's tree at `pins` with reason `unread-notice`. Explicit zero through the daemon and the omitted keyword on the direct path passed. The daemon tests failed on the missing policy argument, the old Boolean returns and the owed-step result after crossing the hourly deadline. The mirror case passed.

## Test commands

Each pytest run used `uv` 0.11.7, pytest 9.1.1 and pytest-xdist 3.8.0. With `run` set to the row's label, the command was:

```sh
uv run --no-sync --offline --with 'pytest-xdist>=3.6' python -m pytest \
  -n 4 --timeout=540 --basetemp "$PWD/build/dnw-build-r1/pytest-$run" -q "${modules[@]}"
```

The fail-first, after-change and mutant runs used these two modules:

```text
tests/unit/test_retention_delete_notice_window.py
tests/unit/test_retention_delete_daemon.py
```

The full run added these 34 modules in order:

```text
tests/unit/test_retention_delete_apply.py
tests/unit/test_retention_delete_admin_census.py
tests/unit/test_retention_delete_admin_marker.py
tests/unit/test_retention_delete_admin_storage.py
tests/unit/test_retention_delete_canary_journal.py
tests/unit/test_retention_delete_census.py
tests/unit/test_retention_delete_commit_preservation.py
tests/unit/test_retention_delete_dangling.py
tests/unit/test_retention_delete_exclusion_boundary.py
tests/unit/test_retention_delete_final_preservation.py
tests/unit/test_retention_delete_legacy.py
tests/unit/test_retention_delete_properties.py
tests/unit/test_retention_delete_request.py
tests/unit/test_retention_delete_request_record.py
tests/unit/test_retention_delete_resume.py
tests/unit/test_retention_delete_root_and_borrowers.py
tests/unit/test_notice_keep.py
tests/fake/test_notice_keep_delivery.py
tests/fake/test_retention_dormant.py
tests/fake/test_review130_ab.py
tests/fake/test_native_maintenance_startup.py
tests/fake/test_admission_ramp_retention.py
tests/fake/test_attempt_tmpdir_daemon.py
tests/fake/test_attempt_tmpdir_leaks_daemon.py
tests/fake/test_unrun_worktrees.py
tests/unit/test_retention_deadline_r2.py
tests/unit/test_timers_retention.py
tests/unit/test_daemon_settle.py
tests/fake/test_state_contract.py
tests/unit/test_invariants_index.py
tests/unit/test_test_module_names.py
tests/unit/test_handler_refusals.py
tests/unit/test_retention_turn_contract.py
tests/unit/test_desktop_ledger.py
```

## Results

The fixed targeted run reported `56 passed in 522.90s (0:08:42)`. Free space and the three load averages below are from `df -H /` and `uptime` immediately before launch. Temp sizes are the final `du -sk` and the sampled peak during the run. Each basetemp was removed before the next run after verifying its resolved parent and `pytest-` prefix.

| Run | Start UTC | Free | Load averages | Pass | Fail | Skip | Final / peak KiB |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| fail-first | 16:25:32 | 76G | 47.91 / 47.87 / 55.84 | 36 | 20 | 0 | 12368 / 12368 |
| fixed (launcher only) | 16:28:04 | 75G | 48.85 / 48.34 / 54.81 | 0 | 0 | 0 | 0 / 0 |
| after-change | 16:31:41 | 75G | 28.89 / 47.44 / 53.79 | 56 | 0 | 0 | 11780 / 12436 |
| m1 | 16:41:12 | 75G | 51.08 / 45.43 / 51.00 | 49 | 7 | 0 | 12216 / 12424 |
| m2 | 16:46:03 | 75G | 70.20 / 61.08 / 56.80 | 52 | 4 | 0 | 11972 / 12416 |
| m3 | 16:53:16 | 75G | 37.57 / 49.44 / 53.89 | 55 | 1 | 0 | 11720 / 11948 |
| m4 | 17:01:03 | 75G | 40.70 / 41.90 / 48.25 | 51 | 5 | 0 | 11364 / 11592 |
| m5 | 17:17:48 | 73G | 65.20 / 76.74 / 66.80 | 55 | 1 | 0 | 11804 / 12500 |
| m6 | 17:29:01 | 73G | 52.48 / 58.97 / 61.55 | 54 | 2 | 0 | 11844 / 12504 |
| m7 | 17:43:33 | 72G | 50.18 / 52.18 / 55.65 | 49 | 7 | 0 | 11804 / 12500 |
| full (partial) | 17:52:37 | 72G | 32.64 / 38.96 / 47.17 | 979 | 11 | 0 | final and peak unavailable |

The 36-module invocation reached 979 passes, 11 failures and zero skips in its progress output by 19:48 UTC. At that point it had produced no pytest summary or failure tracebacks. The latest completed direct basetemp sample was 255296 KiB at 19:14 UTC. The deadline guard was configured to stop the run at 19:45 UTC, and the runner had yet to emit its completion and cleanup record.

### Mutation results

| Mutant | Tests that failed |
| --- | --- |
| M1, omit the keep at `_pinned` | Expired notice (a), exact cutoff (c), dry-run (e), seven-day and default daemon policy (f), and both expired-notice re-asks (g) |
| M2, omit the keep at the daemon | The exact step kwargs and default budget tests, and seven-day and default daemon policy (f) |
| M3, default to seven days | `test_zero_or_omitted_keep_pins_an_old_unread_notice[omitted]` |
| M4, omit the path clock | Fresh notice (b), one second after cutoff (c), dry-run (e), and both fresh-notice re-asks (g) |
| M5, use an early clock reading for the answer | `test_an_owed_step_is_deferred_when_the_pass_becomes_due_during_pending` |
| M6, always report a step as owed | `test_a_catch_up_leaves_a_window_whose_step_settles_the_debt` and `test_a_delete_step_takes_retentions_budget_from_its_defaults` |
| M7, always report a step as free | All four `LEAVE_DUE` cases, the owed step that becomes due during `pending()`, and both worker-clock backoff cases |

## Limits

The delete path writes no outcome service notice. Deleting the tree leaves the job's rows, outputs and existing notice available. An expired notice that previously kept a tree on this path makes the tree eligible for deletion when an operator runs the approved request again, subject to the other gates.

## Workspace and deviations

- The sandbox denied writes to the worktree's shared Git index. The test, code, documentation and report changes are in the assigned worktree for salvage. `git log --oneline a4d21501e..HEAD` has no entries. Mutants were restored from the saved fixed bytes, with byte equality checked after each run, because `HEAD` still points at the base.
- The system temp directory rejected creation of the job folder, and its pre-existing uv cache was read-only here. Tests used the brief's `build/dnw-build-r1` fallback and a cache inside it. The checkout had no virtualenv. Offline sync lacked locked dependencies, so one locked sync fetched them and one small online uv invocation primed xdist. All pytest runs used the resulting offline cache. The first after-change launch, labeled `fixed`, selected Homebrew uv 0.12.13 and failed before pytest started or created a basetemp. The `after-change` run used the same uv 0.11.7 as fail-first.
- The notice test also covers the pre-removal re-ask using the same hook as the pre-rename case. Both hook writes succeeded in the synthetic store.
- Test launches redirected pytest's failed-state directory into the job folder so session setup could leave the shared temp area alone.
- No job folders were created outside the worktree. The test environment and the full-run temporary data were still in the worktree when this report was written.
