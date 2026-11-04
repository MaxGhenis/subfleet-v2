# Catalog temporary-file cleanup

Base: `a8624a4899a2` (`release/217`). Work began 2026-11-04 at 21:07 UTC; all work stays in the assigned job workspace. Implementation commit: `b28784d1525c2d8168c79d4abc4c74a0bac1c2d1`, on `fix/catalog-temp-sweep-217` in workspace-local `.git-local`. The mixed-folder recovery property and the updated signal-handler explanation are committed as `258951a65`. No push or history rewrite.

The incident supplied with the task describes 29 abandoned catalog temporary files totaling 1.1 GB and 2,606 timeout-stop log lines. I did not inspect the host's state or logs. The code explains the leak: `_reap_catalog` used SIGKILL immediately after 60 seconds, bypassing the atomic writer's cleanup. Shutdown could also exhaust its short wait, and crashes and power loss can leave temps independently.

**Changes**

- `subfleet/conversations/service.py`: send SIGTERM after 60 seconds, record its monotonic timestamp, and send SIGKILL only on a later tick after `CATALOG_TERM_GRACE_S = 10.0`. Reaping remains nonblocking and retains the process until reaped, so the group being signaled still belongs to that run. Each signal gets one log line. Shutdown reuses any signal already sent and honors the remaining grace before escalation, then retains its two-second post-SIGKILL wait.
- `subfleet/conversations/catalog.py`: `sweep_temps(root)` scans that directory alone, uses whole-name matching and a no-follow regular-file check, skips errors per entry, and returns the number and byte size of files actually unlinked. `build` checks its Owner before sweeping once, before the first publish, and retains the checks before each publish. There is no dedicated successful-run outcome record, so sweep statistics are returned by the helper for callers/tests and are not added to stdout, the catalog, cache, or a new log/record.
- The signal tests exposed another gap: guardian creates its temp before entering `try/finally`. Catalog's `_atomic` now blocks SIGTERM until that publish has completed its cleanup and restores the previous signal mask in `finally`. A pending SIGTERM then raises the existing `SystemExit`. This covers a signal immediately after temp creation without changing guardian or its other callers. An unresponsive publish can still require SIGKILL; the next completing run repairs its leftovers.
- `tests/unit/test_conversation_catalog.py`: generated exact-sweep coverage, error handling, Owner checks, real lock contention, and before-publish integration.
- `tests/unit/test_conversation_catalog_lifecycle.py`: fake-clock escalation and shutdown coverage, plus real catalog subprocesses stopped at publish phases.
- `tests/unit/test_conversation_service.py`: update existing timeout, shutdown, and generated lifecycle expectations for the grace and signal logs.
- `docs/acceptance-contract.md`: amend C-30.1 and add its required change-log entry in the same commit as code and tests.

`WALL_S`, the refresh interval, catalog/cache contents, and guardian's implementation and other callers are unchanged. There are no new event/record keys or refusal reasons.

**Invariants and tests**

Test names below are in the two catalog test modules unless otherwise stated.

| Invariant | Evidence |
| --- | --- |
| 1. A run ending through SIGTERM leaves no temp of its writes. | `test_sigterm_at_each_atomic_publish_phase_leaves_no_temp`: 14 real `python -m subfleet.conversations.catalog --state-root <test-root>` cases, both publishes at seven phases: temp creation before guardian's cleanup block, before write, partial write, full write, file fsync, rename, and directory fsync. Each exits 143 and leaves no matching temp. The existing `test_a_stopped_run_unwinds_and_leaves_only_its_lock` covers SIGTERM during discovery. |
| 2. After any sequence of SIGKILL-interrupted runs, a completing run leaves no matching file in the root. | `test_completed_run_sweeps_any_sequence_of_sigkill_interruptions`: Hypothesis generates sequences of one to five real catalog processes and publish phases, with up to 12 generated examples and two explicit examples covering every phase of each publish. It interrupts empty, partial, and completed writes before a final real process completes and leaves no matching file. Each run's random tempfile name comes from the real writer. The same property generates initial names, kinds, links, and byte contents, and checks snapshots of protected root entries, nested files, and link targets after each killed run and the final completion. |
| 3. Exactly regular files with the two prefixes and eight `[a-z0-9_]` characters are removed, only in the root. | `test_the_sweep_removes_exactly_regular_temps_in_the_root`: Up to 60 Hypothesis examples of names, contents, and kinds, with guaranteed adversarial examples in every folder. Covers seven/nine-character suffixes, `.tmp`, letter case, invalid characters, empty files, file/directory/dangling links, matching directories and FIFOs, nested files and external link targets, ordinary names, and published files/lock. Compares complete no-follow tree snapshots, count/bytes, and idempotence. `test_the_sweep_skips_an_unremovable_temp_and_goes_on` forces ENOENT/EACCES first and verifies later deletion. |
| 4. A run whose Owner is gone removes and publishes nothing. | `test_a_run_whose_owner_is_gone_neither_sweeps_nor_publishes`, parameterized over a closed fence and replaced lock: preexisting temps and published files stay byte-for-byte intact, and neither sweep nor publish is called. Existing lifecycle tests also exercise real children whose owner closes or whose root disappears/is replaced during discovery. |
| 5. A run failing to take the lock never reaches the sweep. | `test_a_run_that_cannot_take_the_lock_never_reaches_the_sweep`: real exclusive flock contention through `main`, no sweep call, and unchanged folder snapshot. |
| 6. SIGTERM precedes SIGKILL, with no SIGKILL before the grace. | `test_a_wedged_run_gets_sigterm_then_a_full_grace_before_sigkill`: fake clock covers the timeout boundary, a late first tick, repeated ticks, just before/exactly at the grace, one log per signal, no waits, and no signal after reaping. `test_a_run_that_unwinds_on_sigterm_is_reaped_without_sigkill` covers graceful completion. `test_close_honors_the_remaining_sigterm_grace_without_repeating_signals` covers shutdown before TERM or at TERM ages 0/4/10/20 seconds; `test_close_after_the_reaper_sent_sigkill_never_repeats_it` prevents duplicate escalation. |

The SIGTERM guarantee concerns the terminated run's own temporary writes. Leftovers from earlier SIGKILLs are repaired when a permitted run reaches the sweep; a run stopped before taking the lock or after losing its Owner cannot remove those older files while respecting invariants 4 and 5. Owner guarantees apply at the checks, with C-30.1's existing check-to-write race unchanged.

The child-only test seam is loaded through `sitecustomize` and inherited synchronization pipes; it pauses inside the real atomic writer or immediately after its real `mkstemp`, and splits one write to expose a partial file. This replaces flaky random wall-clock sleeps with generated interruption phases. Every child is reaped in `finally`; all homes, provider stores, and state roots are isolated test directories.

**Mutation checks**

Every requested mutant was executed through a temporary in-memory pytest plugin, leaving the checked-out implementation untouched. Failure counts below are expected negative-control outcomes, separate from baseline test results.

| Mutant | Catching test | Observed result |
| --- | --- | --- |
| (a) SIGKILL first | `test_a_wedged_run_gets_sigterm_then_a_full_grace_before_sigkill` | 1 failed |
| (b) No SIGTERM grace | Same fake-clock test | 1 failed |
| (c) Sweep without its Owner check | `test_a_run_whose_owner_is_gone_neither_sweeps_nor_publishes` | 2 failed, fence and replaced lock |
| (d) Wildcard instead of exactly eight characters | `test_the_sweep_removes_exactly_regular_temps_in_the_root` | 1 failed |
| (e) Follow symlinks | Same exact-sweep property | 1 failed |
| (f) Recurse into folders | Same exact-sweep property | 1 failed |
| (g) Remove build's sweep invocation | `test_a_run_sweeps_before_its_first_publish` | 1 failed |

The real SIGKILL-sequence property also requires the sweep integration: its explicit examples end with a dead run's unrenamed temp.

The strengthened mixed-folder property also verifies that a fully fsynced temporary write parses as JSON before signaling.

**Commands and results**

`TASK_TMP` below abbreviates the writable session scratch directory:

```
/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261104-160336-catalog-tmp-sweep-a1-ihqvjtdd
```

Dependency setup attempted `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR="$TMPDIR/uv-cache" uv sync --group dev --extra api`. The cache path is outside the sandbox's writable roots; with a writable cache, the command then reports that `api` is not a defined extra. The successful equivalent for this checkout was:

```sh
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
export UV_CACHE_DIR="$TASK_TMP/uv-cache"
uv sync --group dev
```

Targeted commands used `uv run --no-sync pytest -q` with a fresh `--basetemp "$TASK_TMP/<run-name>"` each time; the service runs used the equivalent uv `--cache-dir "$TASK_TMP/uv-cache"` override.

| Scope | Result |
| --- | --- |
| `tests/unit/test_conversation_catalog.py` after all edits | 22 passed in 11.95 s |
| `tests/unit/test_conversation_catalog_lifecycle.py tests/unit/test_conversation_service.py`, before adding the publish-phase tests | 129 passed in 130.27 s |
| Final fake-clock verification after conservative TERM timestamp adjustment | 8 passed, 21 deselected in 2.72 s |
| Real SIGTERM/SIGKILL publish-phase tests | 15 passed in 84.99 s; executed in a temporary separate signals module before moving identical code into the requested lifecycle module |
| Strengthened mixed-folder property in its final lifecycle module | Included among all 44 passing lifecycle cases in full-suite shard 6 |
| All requested mutants | All seven caught; 8 expected test failures in total |
| Whitespace validation | `git diff --check` passed |

The full-suite run uses Python 3.14.7 and holds `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock` around all 12 shards. It sets `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR="$TMPDIR/uv-cache"`, `SUBFLEET_LIVE=0`, `HYPOTHESIS_PROFILE=ci`, and prepends the workspace's `tools` directory to `PYTHONPATH`. For each `I=1..12`, it executes:

```sh
uv --cache-dir "$TASK_TMP/uv-cache" run --no-sync --with 'pytest-xdist>=3.6' \
  pytest -q -n 4 -p ci_shard_plugin --ci-shard "$I/12" --timeout=540 \
  --basetemp "$TASK_TMP/catalog-full-suite/tmp-shard-$I" \
  --junitxml "$TASK_TMP/catalog-full-suite/shard-$I.xml"
```

All 12 completed shards are recorded below. The full suite is **not green**.

| Shard | Passed | Failed | Errors | Skipped | Xfailed |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1/12 | 86 | 7 | 790 | 0 | 0 |
| 2/12 | 0 | 20 | 2 | 0 | 0 |
| 3/12 | 0 | 0 | 149 | 0 | 0 |
| 4/12 | 1712 | 27 | 5 | 31 | 13 |
| 5/12 | 2108 | 0 | 0 | 54 | 0 |
| 6/12 | 2802 | 9 | 0 | 27 | 1 |
| 7/12 | 2118 | 1 | 0 | 65 | 9 |
| 8/12 | 2519 | 7 | 4 | 57 | 4 |
| 9/12 | 2048 | 8 | 0 | 17 | 18 |
| 10/12 | 3872 | 4 | 9 | 30 | 7 |
| 11/12 | 5413 | 6 | 0 | 18 | 19 |
| 12/12 | 2499 | 13 | 0 | 18 | 31 |
| **Total** | **25177** | **102** | **959** | **317** | **102** |

Aggregate shard wall time: 7316.00 s. Affected modules in this full-suite run: 166 passed.

Failure/error cases by module (counts include propagated fixture errors):

- `tests.fake.test_probe_cancel.TestProbeCancelMachine`: 1.
- `tests.frontend.test_app_cutover_start`: 13.
- `tests.frontend.test_conversation_view`: 6.
- `tests.frontend.test_core_app_cutover_reading`: 15.
- `tests.frontend.test_core_approval_notification`: 10.
- `tests.frontend.test_core_approval_reach`: 41.
- `tests.frontend.test_core_client`: 18.
- `tests.frontend.test_core_diff`: 7.
- `tests.frontend.test_core_draft`: 7.
- `tests.frontend.test_core_idle`: 1.
- `tests.frontend.test_core_live`: 1.
- `tests.frontend.test_core_live_failure`: 1.
- `tests.frontend.test_core_live_steer`: 16.
- `tests.frontend.test_core_markdown`: 42.
- `tests.frontend.test_core_outbox`: 13.
- `tests.frontend.test_core_protocol`: 10.
- `tests.frontend.test_core_question_card`: 10.
- `tests.frontend.test_core_queue_engine`: 75.
- `tests.frontend.test_core_queue_tray`: 69.
- `tests.frontend.test_core_search`: 20.
- `tests.frontend.test_core_steer`: 43.
- `tests.frontend.test_core_steer_properties`: 2.
- `tests.frontend.test_core_store`: 10.
- `tests.frontend.test_core_suggestions`: 7.
- `tests.frontend.test_core_suggestions_r1b`: 4.
- `tests.frontend.test_core_text_scale`: 7.
- `tests.frontend.test_core_timeline`: 23.
- `tests.frontend.test_core_titles`: 1.
- `tests.frontend.test_disk_ruling`: 8.
- `tests.frontend.test_menu_view`: 8.
- `tests.frontend.test_pr124_fixes_ui`: 17.
- `tests.frontend.test_pr124_recovery_properties`: 1.
- `tests.frontend.test_pr124_review_r2_fixes_ui`: 19.
- `tests.frontend.test_queue_tray_fix4_mutations`: 4.
- `tests.frontend.test_queue_tray_notice`: 2.
- `tests.frontend.test_queue_tray_view`: 18.
- `tests.frontend.test_reading_view`: 5.
- `tests.frontend.test_review_pr127_timeline`: 4.
- `tests.frontend.test_review_pr284_pin`: 16.
- `tests.frontend.test_review_pr284_r2`: 6.
- `tests.frontend.test_review_pr284_resizes`: 3.
- `tests.frontend.test_review_r219_fix10_view`: 40.
- `tests.frontend.test_review_r219_fix11_view`: 6.
- `tests.frontend.test_review_r219_fix12_view`: 9.
- `tests.frontend.test_review_r219_fix13_view`: 8.
- `tests.frontend.test_review_r219_fix14_setup`: 4.
- `tests.frontend.test_review_r219_fix4`: 39.
- `tests.frontend.test_review_r219_fix6`: 7.
- `tests.frontend.test_review_r219_fix8_engine`: 8.
- `tests.frontend.test_review_r219_fix8_mutations`: 22.
- `tests.frontend.test_review_r219_fix8_view`: 6.
- `tests.frontend.test_review_r219_fix9_view`: 3.
- `tests.frontend.test_review_r219_probes`: 23.
- `tests.frontend.test_status_model`: 26.
- `tests.frontend.test_visual_progress`: 5.
- `tests.frontend.test_visual_review_fixes`: 43.
- `tests.frontend.test_visual_round_five`: 9.
- `tests.frontend.test_visual_round_four`: 20.
- `tests.frontend.test_visual_round_three`: 24.
- `tests.frontend.test_visual_round_two`: 83.
- `tests.frontend.test_visual_snapshots`: 3.
- `tests.frontend.test_visual_suggestions`: 1.
- `tests.process.test_census_parent_chain_process`: 3.
- `tests.process.test_provider_shell_environment`: 2.
- `tests.unit.test_cpu_scaling`: 1.
- `tests.unit.test_guard_subfleet_root`: 7.
- `tests.unit.test_guard_trust`: 1.
- `tests.unit.test_person_boundary`: 1.
- `tests.unit.test_person_boundary_identity`: 9.
- `tests.unit.test_publish_rule_round7`: 6.
- `tests.unit.test_quar_start1_census`: 3.
- `tests.unit.test_retention_alternates`: 1.
- `tests.unit.test_retention_deadline_r2`: 2.
- `tests.unit.test_retention_dependency_properties`: 4.
- `tests.unit.test_retention_dependency_units`: 4.
- `tests.unit.test_retention_git_common_dir`: 4.
- `tests.unit.test_retention_location_inputs`: 1.
- `tests.unit.test_retention_location_readable`: 2.
- `tests.unit.test_retention_location_review_r13`: 4.
- `tests.unit.test_retention_location_spelling`: 1.
- `tests.unit.test_retention_pointer_inputs`: 2.
- `tests.unit.test_retention_pointer_whitespace`: 1.
- `tests.unit.test_retention_repository_walk`: 25.
- `tests.unit.test_salvage_unindexable`: 4.

The first three shards record 968 Swift failure/error cases caused by `xcrun` being denied cache creation directly under the mandated Darwin temp root. Shard 4 has 25 failures in untouched retention repository-walk tests and 2 guard-hook failures plus 5 guard fixture errors. The retention failures are consistent with an allocation error being swallowed before any ambient Git probe; this is an inference from the code and empty probe traces, not a recorded exception or a baseline comparison. The guard failures wrap the 540-second timeout. Later shards include provider-shell/publish-policy failures and explicit temp-path permission denials. No unrelated implementation was changed to conceal these failures, and their unrestricted baseline status was not established.

Final combined focused check: 166 passed. It used the same uv/xdist command with the three affected test modules, `-n 4`, `--timeout=120`, and a fresh `--basetemp "$TASK_TMP/catalog-final-focused"`; results: `$TASK_TMP/catalog-final-focused.xml`, output: `$TASK_TMP/catalog-final-focused.log`.

Full shard logs, JUnit files, and `results.json` are in `$TASK_TMP/catalog-full-suite/`.


**Deviations and limits**

- The shared Git index/ref directory and `.git` pointer are read-only. Staging and branch creation in the shared metadata were rejected with `Operation not permitted`; the requested branch is also already checked out in the caller's workspace. Commits therefore use the repository's established `.git-local` convention on the exact assigned branch, descending directly from the assigned base, with named-file staging only. The incremental delivery bundle `docs/reports/catalog-temp-sweep.bundle` preserves the commits for the hub without writing into the caller's checkout or shared metadata.
- The requested `api` extra does not exist at this commit. The dev group was synced without it. The required default uv cache is not writable, so uv uses the session-local cache noted above. Test base directories remain new folders beneath the Darwin TMPDIR in the explicitly writable task directory.
- Shutdown now honors ten seconds of TERM grace rather than its previous two seconds before escalation. This is necessary to satisfy invariant 6 for shutdown as well as timeout reaping; the separate post-SIGKILL wait remains two seconds.
- Catalog-only SIGTERM masking is an additional fix required by invariant 1's creation-before-cleanup case. It allows the current atomic publish to finish before the pending signal exits the run. SIGKILL, crashes, and power loss still require the next completing run's sweep.
- Deterministic publish-phase pauses replace timing-dependent random sleeps as the brief permits. The seam exists entirely in tests; guardian's implementation is untouched.
- The GitNexus debugging skill was read, but no GitNexus tools are available in this session; source inspection provided the call trace.

No live state or native store was read or changed; no forbidden command, push, detached background shell, or session-persistent process was started.

**Open questions**

Why so many catalog runs outlive 60 seconds on a loaded host remains unanswered: this job deliberately does not inspect live processes or stores. Cache loading/decoding, directory walks, provider reads, `ps`, JSON serialization, and a large atomic publish are possible locations; the patch leaves the wall cap, interval, and indexed content unchanged. Host-side timing work can identify the dominant cost separately. Unremovable temps are intentionally retained; their count/size return values describe only successful removals, and a later run retries them.

Report finalized 2026-11-04T23:45:28+00:00. Delivery uses `.git-local` branch `fix/catalog-temp-sweep-217`; the bundle prerequisite is `a8624a4899a2ce40c849cc5c6f7d74a7010f11b5`.
