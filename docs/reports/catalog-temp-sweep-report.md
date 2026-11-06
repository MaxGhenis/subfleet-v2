# Catalog temporary-file cleanup

D-CATALOG-TMP1, PR #293, branch `fix/catalog-temp-sweep-217`, based on `a8624a4899a2` (`release/217`). The PR changes catalog termination and temporary-file cleanup; the initial implementation and tests were recorded in `b28784d1525c2d8168c79d4abc4c74a0bac1c2d1`, with the mixed-folder recovery property strengthened in `258951a65`. The initial validation below was completed on 2026-11-04; the Fix round 1 section records the subsequent review changes and checks.

The incident supplied with the task describes 29 abandoned catalog temporary files totaling 1.1 GB and 2,606 timeout-stop log lines. I did not inspect the host's state or logs. The code explains the leak: `_reap_catalog` used SIGKILL immediately after 60 seconds, bypassing the atomic writer's cleanup. Shutdown could also exhaust its short wait, and crashes and power loss can leave temps independently.

**Changes**

- `subfleet/conversations/service.py`: send SIGTERM after 60 seconds, record its monotonic timestamp, and send SIGKILL only on a later tick after `CATALOG_TERM_GRACE_S = 10.0`. Reaping remains nonblocking and retains the process until reaped, so the group being signaled still belongs to that run. Each signal gets one log line. Daemon shutdown starts the catalog stop before its other waits; service close reuses any signal already sent and waits only the remaining grace before escalation, then retains its two-second post-SIGKILL wait.
- `subfleet/conversations/catalog.py`: `sweep_temps(root)` scans that directory alone, uses whole-name matching and a no-follow regular-file check, skips errors per entry, and returns the number and byte size of files actually unlinked. `build` checks its Owner before sweeping once, before the first publish, and retains the checks before each publish. There is no dedicated successful-run outcome record, so sweep statistics are returned by the helper for callers/tests and are not added to stdout, the catalog, cache, or a new log/record.
- The signal tests exposed another gap: guardian creates its temp before entering `try/finally`. Catalog's `_atomic` defers termination until that publish has completed its cleanup. The SIGTERM handler sets a pending flag while `_atomic` runs, and `_atomic` then raises the existing `SystemExit(143)` after cleanup, independently of the run's thread count. This covers a signal immediately after temp creation without changing guardian or its other callers. An unresponsive publish can still require SIGKILL; the next completing run repairs its leftovers. Fix round 1 replaces the original thread-local signal mask with this pending flag.
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
| 3. Exactly regular files with the two prefixes and eight `[a-z0-9_]` characters are removed, only in the root. | `test_the_sweep_removes_exactly_regular_temps_in_the_root`: Up to 60 Hypothesis examples of names, contents, and kinds, with guaranteed adversarial examples in every folder. Covers seven/nine-character suffixes, `.tmp`, letter case, invalid characters, empty files, file/directory/dangling links, matching directories and FIFOs, nested files and external link targets, ordinary names, and published files/lock. Compares complete no-follow tree snapshots, count/bytes, and idempotence. `test_the_sweep_skips_an_unremovable_temp_and_goes_on` forces ENOENT/EACCES from `unlink` first and verifies later deletion. |
| 4. A run whose Owner is gone removes and publishes nothing. | `test_a_run_whose_owner_is_gone_neither_sweeps_nor_publishes`, parameterized over a closed fence and replaced lock: preexisting temps and published files stay byte-for-byte intact, and neither sweep nor publish is called. Existing lifecycle tests also exercise real children whose owner closes or whose root disappears/is replaced during discovery. |
| 5. A run failing to take the lock never reaches the sweep. | `test_a_run_that_cannot_take_the_lock_never_reaches_the_sweep`: real exclusive flock contention through `main`, no sweep call, and unchanged folder snapshot. |
| 6. SIGTERM precedes SIGKILL, with no SIGKILL before the grace. | `test_a_wedged_run_gets_sigterm_then_a_full_grace_before_sigkill`: fake clock covers the timeout boundary, a late first tick, repeated ticks, just before/exactly at the grace, one log per signal, no waits, and no signal after reaping. `test_a_run_that_unwinds_on_sigterm_is_reaped_without_sigkill` covers graceful completion. `test_close_honors_the_remaining_sigterm_grace_without_repeating_signals` covers shutdown before TERM or at TERM ages 0/4/10/20 seconds; `test_close_after_the_reaper_sent_sigkill_never_repeats_it` prevents duplicate escalation. |

The SIGTERM guarantee concerns the terminated run's own temporary writes. Leftovers from earlier SIGKILLs are repaired when a permitted run reaches the sweep; a run stopped before taking the lock or after losing its Owner cannot remove those older files while respecting invariants 4 and 5. Owner guarantees apply at the checks: both the sweep and publish act by path after their checks, so a root replacement can race either operation, as C-30.1 states.

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

The initial dependency setup attempted `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR="$TMPDIR/uv-cache" uv sync --group dev --extra api`. That checkout has no `api` extra; `uv sync --group dev` succeeded with a writable temporary uv cache. Tests used fresh temporary directories beneath the Darwin TMPDIR. The following command template describes those targeted runs without retaining build-specific paths:

```sh
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
export UV_CACHE_DIR="$TMPDIR/uv-cache"
TEST_ROOT="$(mktemp -d "$TMPDIR/catalog-temp-sweep.XXXXXX")"
uv run --no-sync pytest -q --basetemp "$TEST_ROOT/targeted" <test-files>
```

| Scope | Result |
| --- | --- |
| `tests/unit/test_conversation_catalog.py` after all edits | 22 passed in 11.95 s |
| `tests/unit/test_conversation_catalog_lifecycle.py tests/unit/test_conversation_service.py`, before adding the publish-phase tests | 129 passed in 130.27 s |
| Final fake-clock verification after conservative TERM timestamp adjustment | 8 passed, 21 deselected in 2.72 s |
| Real SIGTERM/SIGKILL publish-phase tests | 15 passed in 84.99 s; executed in a temporary separate signals module before moving identical code into the requested lifecycle module |
| Strengthened mixed-folder property in its final lifecycle module | Included among all 44 passing lifecycle cases in full-suite shard 6 |
| All requested mutants | All seven caught; 8 expected test failures in total |
| Whitespace validation | `git diff --check` passed |

The initial full-suite run used Python 3.14.7 and serialized all 12 shards under the test-suite lock. It set `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"`, `UV_CACHE_DIR="$TMPDIR/uv-cache"`, `SUBFLEET_LIVE=0`, `HYPOTHESIS_PROFILE=ci`, and prepended the repository's `tools` directory to `PYTHONPATH`. For each `I=1..12`, it executed the following template with a fresh temporary root:

```sh
uv run --no-sync --with 'pytest-xdist>=3.6' \
  pytest -q -n 4 -p ci_shard_plugin --ci-shard "$I/12" --timeout=540 \
  --basetemp "$TEST_ROOT/full-suite/tmp-shard-$I" \
  --junitxml "$TEST_ROOT/full-suite/shard-$I.xml"
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

Failure/error cases by area (including propagated fixture errors):

| Area | Cases |
| --- | ---: |
| Frontend compiler/UI checks | 972 |
| Provider-shell/publication-policy checks | 8 |
| Retention checks | 51 |
| Other process, identity, resource, salvage, and fake-probe checks | 22 |
| Guard-hook checks | 8 |

The first three shards record 968 Swift failure/error cases caused by `xcrun` being denied cache creation directly under the mandated Darwin temp root. Shard 4 has 25 failures in untouched retention repository-walk tests and 2 guard-hook failures plus 5 guard fixture errors. The retention failures are consistent with an allocation error being swallowed before any ambient Git probe; this is an inference from the code and empty probe traces, not a recorded exception or a baseline comparison. The guard failures wrap the 540-second timeout. Later shards include provider-shell/publish-policy failures and explicit temp-path permission denials. No unrelated implementation was changed to conceal these failures, and their unrestricted baseline status was not established.

Final combined focused check: **166 passed in 90.72 s**. It used the same uv/xdist command with the three affected test modules, `-n 4`, `--timeout=120`, and a fresh temporary `--basetemp`; its JUnit result and captured output were temporary validation artifacts.

The shard logs, JUnit files and aggregate results were temporary validation artifacts, not PR deliverables.

**Deviations and limits**

- The requested `api` extra does not exist at this commit. The initial validation synced the dev group without it and used a writable temporary cache; each test run had a fresh base directory beneath Darwin TMPDIR.
- Shutdown honors ten seconds of TERM grace rather than its previous two seconds before escalation. Fix round 1 starts that grace before the daemon's other waits and waits only its remainder at service close, preserving the stop budget; the separate post-SIGKILL wait remains two seconds.
- Catalog-only SIGTERM deferral is an additional fix required by invariant 1's creation-before-cleanup case. It allows the current atomic publish to finish before the pending signal exits the run. Fix round 1 uses a pending flag rather than a thread-local signal mask. SIGKILL, crashes, and power loss still require the next completing run's sweep.
- Deterministic publish-phase pauses replace timing-dependent random sleeps as the brief permits. The seam exists entirely in tests; guardian's implementation is untouched.
- The GitNexus debugging skill was read, but no GitNexus tools are available in this session; source inspection provided the call trace.

No live state or native store was read or changed; no forbidden command, push, detached background shell, or session-persistent process was started.

**Open questions**

Why so many catalog runs outlive 60 seconds on a loaded host remains unanswered: this job deliberately does not inspect live processes or stores. Cache loading/decoding, directory walks, provider reads, `ps`, JSON serialization, and a large atomic publish are possible locations; the patch leaves the wall cap, interval, and indexed content unchanged. Host-side timing work can identify the dominant cost separately. Unremovable temps are intentionally retained; their count/size return values describe only successful removals, and a later run retries them.

Initial validation report finalized 2026-11-04T23:50:26+00:00.

## Fix round 1

PR #293, D-CATALOG-TMP1; reviewed head `354f09aa2796`, validation on 2026-11-05.

- **P2-1:** Added `ConversationService.begin_close()` without a child wait. Round 2 found that its lock still waited behind a spawn, so the original nonblocking claim was too broad. At the top of `Daemon.close()`, after arming the stop watchdog and before its first other wait, it closes the catalog fence and sends SIGTERM for a tracked run. Service close waits only the unused portion of that same grace, then sends SIGKILL if necessary and waits at most 2 s. Repeated calls neither resend TERM nor restart its deadline. A run already reaped while closing is cleared quietly. `_reap_catalog` and its 60 s timeout/10 s grace are unchanged. The order regression failed on the reviewed implementation and passes with the fix; startup contention is addressed in Fix round 2.
- **P3-1:** Replaced `pthread_sigmask` with an active-publish depth and pending TERM flag. `_terminated` sets the flag during a publish; the outermost `_atomic` raises `SystemExit(143)` after cleanup. The deterministic extra-thread probe is now a regression for both catalog files. Both cases leaked temps on the reviewed implementation on Python 3.12.14 and 3.14.7; both pass after the change. Error and nested-publish tests also verify cleanup and reset of the deferral state.
- **P3-2:** Added ENOENT and EACCES cases in which a matching entry's no-follow stat fails before a removable temp. Both verify that the later temp is removed and counted; M3 fails both during the call phase.
- **P3-3:** Added direct successor-run cases after SIGTERM and SIGKILL. Each new run receives its own literal 60 s, SIGTERM first, and a full literal 10 s grace. M2 fails both call-phase assertions; cleanup prevents incidental teardown errors.
- **P3-4:** Added D-CATALOG-TMP1, PR #293 and the report path to the contract change log and removed the splitting blank line. C-30.1 explains pending-flag deferral and that sweep and publish act by path after their checks. The report now describes the PR and distinguishes historical validation from this round.

C-5.8a now records the named worst-case sum: `max(10, 2 + 2 + 15 + 2) + 2 + 5 = 28 s <= STOP_GRACE_S (30 s)`. The maximum overlaps catalog grace with wait answers, the control join, probe containment and reader joins; the final terms are post-SIGKILL and runner waits. The stop-budget test pins this arithmetic and the 60/10 constants. Census, filesystem work and pool drains still rely on the stop watchdog; 28 s is the sum of named bounds, not a measured full-daemon duration.

All 12 requested mutants were run through temporary pytest plugins that alter functions in memory, without editing the implementation on disk. All were killed in the call phase: **17 expected failures, 1 passing control case, no teardown errors**.

| Mutant | Killing test | Result |
| --- | --- | --- |
| (a) SIGKILL first | `test_a_wedged_run_gets_sigterm_then_a_full_grace_before_sigkill` | 1 failed |
| (b) No grace | Same test | 1 failed |
| (c) Remove pre-sweep Owner check | `test_a_run_whose_owner_is_gone_neither_sweeps_nor_publishes` | 2 failed |
| (d) Wildcard suffix | `test_the_sweep_removes_exactly_regular_temps_in_the_root` | 1 failed |
| (e) Follow symlinks | Same exact-sweep property | 1 failed |
| (f) Recurse | Same exact-sweep property | 1 failed |
| (g) Drop build's sweep | `test_a_run_sweeps_before_its_first_publish` | 1 failed |
| M2: Drop TERM-time reset | `test_each_catalog_run_gets_its_own_sixty_seconds_and_sigterm_first`, both predecessor signals | 2 failed |
| M3: Move stat outside inner try | `test_the_sweep_skips_a_temp_that_cannot_be_statted_and_goes_on`, ENOENT and EACCES | 2 failed |
| Send TERM after other waits | `test_c5_8a_catalog_sigterm_goes_out_before_the_first_other_wait` | 1 failed |
| Wait full grace at service close | `test_begin_close_spends_the_catalog_grace_while_the_daemon_drains`, elapsed 4/10/21 s | 3 failed, 1 passed (elapsed 0) |
| Drop pending flag's raise | `test_nested_publish_defers_sigterm_until_the_outer_cleanup` | 1 failed |

Commands below normalize temporary paths as `$VALIDATION_DIR`, an existing writable directory beneath Darwin TMPDIR. Every invocation used a new `--basetemp` child. No temporary logs or mutation plugins are PR deliverables.

```sh
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
export UV_CACHE_DIR="$TMPDIR/uv-cache"
uv --cache-dir "$VALIDATION_DIR/uv-cache" sync --group dev

DEFERRAL_CASES='sigterm_from_another_thread_at_temp_creation or sigterm_at_each_atomic_publish_phase or atomic_error_cleans_temp or nested_publish_defers_sigterm'
uv --cache-dir "$VALIDATION_DIR/uv-cache" run --no-sync python -m pytest -q \
  tests/unit/test_conversation_catalog_lifecycle.py -k "$DEFERRAL_CASES" \
  --basetemp "$VALIDATION_DIR/deferral314-fixed"
uv --cache-dir "$VALIDATION_DIR/uv-cache" run --no-project --python 3.12 \
  --with pytest --with hypothesis --with pytest-timeout python -m pytest -q \
  tests/unit/test_conversation_catalog_lifecycle.py -k "$DEFERRAL_CASES" \
  --basetemp "$VALIDATION_DIR/deferral312-fixed"

uv --cache-dir "$VALIDATION_DIR/uv-cache" run --no-sync --with 'pytest-xdist>=3.6' \
  pytest -q -n 4 --timeout=180 \
  tests/unit/test_conversation_catalog.py \
  tests/unit/test_conversation_catalog_lifecycle.py \
  tests/unit/test_conversation_service.py tests/unit/test_stop_watchdog.py \
  tests/unit/test_daemon_verbs.py tests/unit/test_daemon_log_sites.py \
  tests/fake/test_state_contract.py tests/unit/test_test_module_names.py \
  tests/unit/test_invariants_index.py \
  --basetemp "$VALIDATION_DIR/catalog-r1-final" \
  --junitxml "$VALIDATION_DIR/catalog-r1-final.xml"
```

The stat regression used `pytest -q tests/unit/test_conversation_catalog.py -k cannot_be_statted`; shutdown/reset checks used `pytest -q tests/unit/test_conversation_service.py tests/unit/test_stop_watchdog.py -k 'begin_close_spends or each_catalog_run_gets or catalog_sigterm_goes or the_grace_outlasts or close_arms'`, with the same uv cache override and fresh bases. Mutation runs added the temporary plugin to `PYTHONPATH` and selected each tabled test with `-p catalog_deferral_mutants` plus `SF_CATALOG_MUTANT` for (a), (b) and the pending raise; `-p catalog_sweep_mutants --catalog-mutant <c-g>`; `-p catalog_stat_m3` for M3; or `-p catalog_stop_mutants --catalog-stop-mutant <M2|late-term|full-grace>`.

| Check | Result |
| --- | --- |
| Threaded regression before fix, Python 3.12.14 / 3.14.7 | 2 failed on each version, both leaked temps |
| Shutdown-order regression before fix | 1 failed, 29 deselected |
| Signal phases, threaded regression, error and nesting checks, Python 3.12.14 | 23 passed, 26.68 s |
| Same focused checks, Python 3.14.7 | 23 passed, 24.32 s |
| New stat regression | 2 passed, 22 deselected, 1.07 s |
| Corrected shutdown/reset focused checks | 9 passed, 127 deselected, 7.40 s |
| All requested mutants | 12/12 killed; 17 expected failures, 1 passing control, no errors |
| Required final nine files, Python 3.14.7, `-n 4` | **373 passed, 7 skipped, 274.64 s** |
| `git diff --check` | Passed |

The final run includes 24 catalog, 53 lifecycle, 107 service, 30 stop-watchdog, 15 daemon-verbs, 81 daemon-log-sites, 37 state-contract, 1 module-name and 25 invariant-index passing cases. Five daemon-verbs and two daemon-log-sites cases skip because sandboxed macOS boot/process identity inspection is unavailable. An intermediate focused run had four test-harness failures because the existing fake-run fixture patches the post-KILL wait to zero; the new test now explicitly restores 2 s before asserting it. The whole suite was left to CI as requested.

Deviations: the requested `uv sync --group dev --extra api` could not complete: the mandated default cache is unwritable, and retrying with the writable cache confirms that this checkout defines no `api` extra. `uv --cache-dir "$VALIDATION_DIR/uv-cache" sync --group dev` succeeded; tests retained the required TMPDIR/UV_CACHE_DIR exports and used the explicit writable cache override. No dependency manifest changed.

Named-file staging was attempted after each coherent step. Git refused creation of the shared worktree's `index.lock` with `Operation not permitted`, so no commit could be made; all nine changed files, including this report, remain in the assigned workspace for hub collection. No alternate Git metadata, history rewrite or push was used. No live state was read or changed, no new event/record keys or refusals were introduced, and no whole-suite run or persistent background work was started. Work completed within the three-hour limit.

## Fix round 2

Reviewed head: `b7500b81794f`, PR #293. Work is confined to the assigned worktree and its permitted test scratch directory.

### Findings and design

- **P2 — startup contention:** `_start_catalog()` reserves the single start under `_catalog_lock`, then creates the fence and spawns outside it. An in-flight start counts as running. `begin_close()` uses short state-only critical sections: no spawn, child wait, descriptor close, logging or signal send occurs under its lock. A closing starter publishes the run into service tracking, takes responsibility for its stop, closes both fence descriptors exactly once after spawn returns, sends TERM, and records the end of the send. Shutdown can set `stopping` and timer cancellation while spawn is held. Repeated close calls cannot steal the descriptors or duplicate TERM.
- **Bounded handoff:** `CATALOG_HANDOFF_WAIT_S = 1.0` is an absolute deadline from the initial close claim, not a fresh wait after draining. `_stop_catalog()` waits only its remainder before waiting on a run. A starter that misses the deadline owns the late run's full grace, escalation and reaping itself; the daemon does not append a fresh ten-second grace after its drain. The fence remains open until spawn can no longer inherit it, including on this late path. This ownership fallback is necessary: an arbitrarily stalled spawn cannot both delay shutdown indefinitely and preserve a finite named budget.
- **P3 — omitted joins:** `LockWatch.begin_stop()` signals its existing event at the top of daemon close and starts the unchanged two-second join deadline (`STOP_JOIN_S`). The final join waits only the remainder. Periodic lock-hold sampling stops during the drain once an in-progress sample finishes; watched-lock release/wait reports remain attached until the final join. The stricter real-close test also exposed `Store.close()`'s previously uncounted five-second reader wait. Daemon close now supplies an earlier absolute deadline for that wait; standalone store close retains its default five seconds, and an active reader still safely closes its own connection when it returns. C-3.7 documents this additional necessary change.
- **N2 coverage:** a TERM sender advances the fake clock by 0.5 s. The new test requires `_catalog_terminated_at == 1000.5` and prohibits KILL before the full grace after that timestamp. Deleting the post-send timestamp assignment fails in the call phase.
- **Documentation:** C-5.8a and C-30.1 describe reservation, descriptor ownership, late cleanup and overlapping deadlines. The round-1 report no longer calls the startup-locking implementation nonblocking.

The named sum is **`max(1 + 10, 2 + 2 + 15 + 2, 2, 5) + 2 + 5 = 28 s`**, leaving **2 s under `STOP_GRACE_S = 30 s`**. The terms are handoff plus catalog grace, answer/control/probe/reader drain, LockWatch join, store-reader deadline, post-KILL wait and runner join. The test executes real `Daemon.close()` under a fake clock, spends every modelled remaining timeout, and rejects unmodelled bounded event/condition/thread/process waits. It checks tracked and in-flight starts. No change was made to the 30 s grace, 33 s kernel deadline, 40 s backstops, catalog's 60 s timeout or 10 s TERM grace. Filesystem work, lock acquisition and pool drains still depend on the watchdog; this is not a finite bound on all shutdown work.

### Tests and mutants

The event-held startup regression fails with the original HEAD shutdown/start/reap methods installed, and passes with the new handoff. Fixed rows cover close before lock acquisition, inside spawn, after spawn before handoff, and after handoff. They check no second start, TERM, reaping, both descriptors' single close, and absence of EBADF. Separate rows exercise zero, partial and exhausted handoff deadlines, including late-starter cleanup. LockWatch and store-reader tests cover expired deadlines without resetting them or closing an active reader's connection.

`budget` below means `test_c5_8a_the_grace_outlasts_probe_containment_and_the_backstops_outlast_the_grace`; `boundary` means `test_close_at_each_catalog_start_boundary[in-spawn]`.

| Mutant | Killing test | Call-phase failures |
| --- | --- | --- |
| M2: remove per-run TERM timestamp reset | `test_each_catalog_run_gets_its_own_sixty_seconds_and_sigterm_first` | 2 |
| M3: stat outside the per-entry try | `test_the_sweep_skips_a_temp_that_cannot_be_statted_and_goes_on` | 2 |
| N1: inner publish raises pending TERM | `test_nested_publish_defers_sigterm_until_the_outer_cleanup` | 1 |
| N2: delete post-send timestamp update | `test_begin_close_grace_starts_after_a_slow_term_send` | 1 |
| N3: omit fence read-end close | `test_begin_close_spends_the_catalog_grace_while_the_daemon_drains` | 4 |
| Hold catalog lock across spawn | `test_begin_close_during_start_is_nonblocking` | 1 |
| Drop starter's TERM | `boundary` | 1 |
| Close fence while spawn is in flight | `boundary` | 1 |
| Delay LockWatch stop until final join | `budget` (observes 30 s) | 2 |
| Omit LockWatch's join from the fake waits | `budget` (missing wait receipt) | 2 |
| Omit early store-reader deadline | `budget` (observes 33 s) | 2 |
| Add an unmodelled bounded event wait | `budget` (rejects the wait) | 2 |

**12/12 mutants killed, 21 expected call-phase failures, no teardown errors in the credited runs.** Mutations were installed in memory by a temporary pytest plugin, never written over production files. An initial M3 plugin indentation match was corrected before its credited rerun. Budget mutants were rerun after the stricter guard exposed the store-reader wait; only those final, causally correct failures are credited.

### Commands and results

All tests export `TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"` and `UV_CACHE_DIR="$TMPDIR/uv-cache"`. Here `V` is the permitted Darwin-temp attempt directory's `catalog-r2` child. The default cache and a top-level `mktemp` there were denied by the sandbox, so commands use `uv --cache-dir "$V/uv-cache"` and fresh `--basetemp "$V/<run>"` directories. `uv sync --group dev --extra api` was attempted; the writable-cache retry confirmed this checkout has no `api` extra. `uv sync --group dev` succeeded after retrying one transient Pygments download timeout. A separate `UV_PROJECT_ENVIRONMENT="$V/py312"` was synced with `/opt/homebrew/bin/python3.12`; the workspace environment uses Python 3.14.7.

- Focused shutdown/LockWatch command: `uv run --no-sync pytest -q tests/unit/test_conversation_service.py tests/unit/test_stop_watchdog.py tests/unit/test_lockwatch.py -k 'begin_close or catalog_start_boundary or start_handoff or the_grace_outlasts or lockwatch'`: **33 passed, 131 deselected** before the additional deadline rows.
- Deadline-focused command added `tests/unit/test_store_readers.py` and selected `the_grace_outlasts or start_handoff or first_stop_deadline or close_does_not_wait_forever`: **11 passed, 188 deselected**. The stricter guard first produced two expected failures exposing the omitted store wait; these passed after the deadline fix.
- Baseline and mutants: `PYTHONPATH="$V:$PWD" CATALOG_R2_MUTANT=<name> uv run --no-sync pytest -q <table-test> -p catalog_r2_mutants`, with fresh bases. Two baseline runs each produced one expected startup call-phase failure; the final run restores HEAD's daemon close and catalog start/reap/stop methods. The credited mutant counts are above.
- Timeout repetition command loops serially over Python 3.12.14 and 3.14.7, 20 iterations each: `uv run --no-sync pytest -q 'tests/unit/test_conversation_catalog_lifecycle.py::test_sigterm_at_each_atomic_publish_phase_leaves_no_temp[created-catalog-cache.json]'`, with a fresh base and log for every iteration. **20/20 passed on each Python, 40/40 total** (712.20 and 625.50 summed pytest seconds respectively). The readiness wait and publication seam are unchanged; the reported timeout did not recur.
- Required end-of-work command: `uv run --no-sync --with 'pytest-xdist>=3.6' pytest -q -n 4` with all nine requested files plus `tests/unit/test_lockwatch.py tests/unit/test_store_readers.py`: **426 passed, 7 skipped, 9 failed, 1388.67 s**. Eight failures exposed an old `object.__new__` lifecycle fixture that omitted the new handoff fields; the fixture now initializes them. The ninth was the existing WAL-size assertion (`453232` bytes instead of over 2 MiB), before that test calls close.
- The fresh final run of the same eleven files, with `-n 2` to reduce load, finished **434 passed, 7 skipped, 1 failed, 1145.20 s**. **All nine requested files passed: 383 passed, 7 skipped.** The additional LockWatch file passed all 24 cases; the additional store-reader file passed 27 and failed only the unchanged WAL test (this time `1656272` bytes). No shutdown, catalog, budget or new deadline regression failed.
- Additional WAL diagnosis: a serial retry failed once in **68.79 s**. A temporary probe restoring HEAD's original `Store.close()` passed the case in **16.17 s**, with the snapshot released normally after **9.566 s**. A current-code probe failed in **60.12 s** and directly observed the helper's 30 s hold expiring after **30.173 s**, with `release` still false and no read in use before the write loop finished. That invalidates the intended held-snapshot setup under this host's load; its failure is before `Store.close()`. The repository's WAL test and its holding helper are unchanged, as unrelated to this brief. The probes live only in scratch; invocation was `pytest -q -s tests/unit/test_store_readers.py::test_a_long_snapshot_holds_the_wal_back_without_holding_a_commit_and_it_shrinks_after -p wal_hold_probe`, with the same uv/cache/base overrides.
- Final Python 3.12 focused command selects the startup, grace, handoff, real-close budget, LockWatch, store-reader and corrected reaper-fixture regressions across their five modules: **30 passed, 222 deselected, 140.20 s**. After the full run, the four fixed boundary rows were strengthened to require an explicit successful reap/poll receipt after TERM, then rerun on both Pythons: **4 passed on 3.12.14 (21.12 s), 4 passed on 3.14.7 (9.71 s)**.
- `git diff --check` passed. All test invocations completed; the full repository suite remains for CI.

Named-file staging/commit attempts after coherent steps were refused because Git cannot create the shared worktree's `index.lock` (`Operation not permitted`). No history was rewritten, no replacement Git metadata or branch was created, and nothing was pushed. All eleven changed files remain for hub collection. Additional scope beyond the brief is the store-reader deadline required by the stricter budget test, with its regression and C-3.7 wording; the additional unchanged WAL test's setup failure is reported rather than fixed. No new event/record keys or refusals were added, no live state was read or modified, and no full-suite run or persistent background work was started. Work and reporting completed within the three-hour limit on 2026-11-05.
