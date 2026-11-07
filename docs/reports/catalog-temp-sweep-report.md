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

- **P2 — startup contention:** `_start_catalog()` reserves the single start under `_catalog_lock`, then creates the fence and spawns outside it. An in-flight start counts as running. `begin_close()` uses short state-only critical sections: no spawn, child wait, descriptor close, logging or signal send occurs under its lock. In round 2 a closing starter published the run into service tracking, took responsibility for its stop, closed both fence descriptors after spawn returned, sent TERM, and recorded the end of the send. Fix round 3 below corrects the retained-writer window: close revokes the writer immediately and only the read end stays with the starter. Shutdown can set `stopping` and timer cancellation while spawn is held. Repeated close calls cannot steal the descriptors or duplicate TERM.
- **Bounded handoff:** `CATALOG_HANDOFF_WAIT_S = 1.0` is an absolute deadline from the initial close claim, not a fresh wait after draining. `_stop_catalog()` waits only its remainder before waiting on a run. A starter that misses the deadline owns the late run's full grace, escalation and reaping itself; the daemon does not append a fresh ten-second grace after its drain. Round 2 retained both fence ends until spawn could no longer inherit the read end, including on this late path; Fix round 3 proves that retaining its writer allowed late writes and revokes that writer at the close claim. This ownership fallback is necessary: an arbitrarily stalled spawn cannot both delay shutdown indefinitely and preserve a finite named budget.
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
- Additional WAL diagnosis: a serial retry failed once in **68.79 s**. A temporary probe restoring HEAD's original `Store.close()` passed the case in **16.17 s**, with the snapshot released normally after **9.566 s**. A current-code probe failed in **60.12 s** and directly observed the helper's 30 s hold expiring after **30.173 s**, with `release` still false and no read in use before the write loop finished. That invalidates the intended held-snapshot setup under this host's load; its failure is before `Store.close()`. The repository's WAL test and its holding helper are unchanged, as unrelated to this brief. These uncontrolled single-run observations do not establish a close-version effect; the alternating Fix round 3 comparison below supersedes any attribution. The probes live only in scratch; invocation was `pytest -q -s tests/unit/test_store_readers.py::test_a_long_snapshot_holds_the_wal_back_without_holding_a_commit_and_it_shrinks_after -p wal_hold_probe`, with the same uv/cache/base overrides.
- Final Python 3.12 focused command selects the startup, grace, handoff, real-close budget, LockWatch, store-reader and corrected reaper-fixture regressions across their five modules: **30 passed, 222 deselected, 140.20 s**. After the full run, the four fixed boundary rows were strengthened to require an explicit successful reap/poll receipt after TERM, then rerun on both Pythons: **4 passed on 3.12.14 (21.12 s), 4 passed on 3.14.7 (9.71 s)**.
- `git diff --check` passed. All test invocations completed; the full repository suite remains for CI.

Named-file staging/commit attempts after coherent steps were refused because Git cannot create the shared worktree's `index.lock` (`Operation not permitted`). No history was rewritten, no replacement Git metadata or branch was created, and nothing was pushed. All eleven changed files remain for hub collection. Additional scope beyond the brief is the store-reader deadline required by the stricter budget test, with its regression and C-3.7 wording; the additional unchanged WAL test's setup failure is reported rather than fixed. No new event/record keys or refusals were added, no live state was read or modified, and no full-suite run or persistent background work was started. Work and reporting completed within the three-hour limit on 2026-11-05.

## Fix round 3

Reviewed production head: `e50cf8996f94`, PR #293, D-CATALOG-TMP1. Validation started 2026-11-07 05:07 UTC, confined to this workspace and permitted test scratch. Commands below normalize the permitted attempt directory as `A` and its `comparison-r3` child as `V`; all essential receipts are preserved here because scratch may disappear.

### Step 1 — review question 4

The earlier tree is `b7500b817`, extracted with `git archive` into a new temporary directory, without adding a worktree. The head tree remained unchanged throughout this comparison. Runs were serial, alternating earlier/head: ten WAL runs per tree followed by five daemon-start logging runs per tree. Each invocation used a fresh pytest base. Wall seconds include uv/pytest startup; pytest seconds are its reported duration. Loads are the `uptime` 1/5/15-minute values before and after each invocation. `wal` denotes `test_a_long_snapshot_holds_the_wal_back_without_holding_a_commit_and_it_shrinks_after`; `daemon` denotes `test_under_daemon_start_an_uncaught_exception_reaches_daemon_log_scrubbed`.

| Test | Pair | Tree | Result | Wall s | Pytest s | Load before | Load after |
| --- | ---: | --- | --- | ---: | ---: | --- | --- |
| wal | 1 | earlier | failed | 120.79 | 87.21 | 109.73 103.52 79.43 | 91.39 99.06 80.84 |
| wal | 1 | head | passed | 66.89 | 41.30 | 91.39 99.06 80.84 | 60.67 87.85 77.92 |
| wal | 2 | earlier | passed | 26.86 | 18.34 | 60.67 87.85 77.92 | 57.55 84.63 77.11 |
| wal | 2 | head | passed | 20.26 | 8.71 | 57.55 84.63 77.11 | 56.76 82.65 76.57 |
| wal | 3 | earlier | passed | 22.50 | 14.88 | 56.76 82.65 76.57 | 52.40 80.15 75.82 |
| wal | 3 | head | passed | 40.66 | 20.31 | 49.48 79.09 75.47 | 44.90 73.51 73.56 |
| wal | 4 | earlier | passed | 42.55 | 31.76 | 44.90 73.51 73.56 | 52.11 71.75 72.91 |
| wal | 4 | head | passed | 31.40 | 17.47 | 52.11 71.75 72.91 | 64.64 72.57 73.15 |
| wal | 5 | earlier | passed | 55.21 | 41.41 | 64.64 72.57 73.15 | 70.94 72.79 73.18 |
| wal | 5 | head | passed | 62.86 | 46.37 | 70.94 72.79 73.18 | 69.40 71.93 72.83 |
| wal | 6 | earlier | passed | 38.27 | 22.41 | 69.40 71.93 72.83 | 64.25 70.02 72.06 |
| wal | 6 | head | passed | 49.00 | 34.95 | 64.25 70.02 72.06 | 33.26 60.67 68.49 |
| wal | 7 | earlier | passed | 37.27 | 24.07 | 33.26 60.67 68.49 | 48.85 60.84 68.19 |
| wal | 7 | head | passed | 44.87 | 24.54 | 48.85 60.84 68.19 | 65.55 63.40 68.71 |
| wal | 8 | earlier | passed | 47.05 | 34.40 | 65.55 63.40 68.71 | 57.76 61.70 67.82 |
| wal | 8 | head | passed | 36.75 | 21.24 | 57.76 61.70 67.82 | 49.28 59.02 66.55 |
| wal | 9 | earlier | passed | 44.71 | 26.59 | 49.28 59.02 66.55 | 53.87 58.82 66.09 |
| wal | 9 | head | passed | 40.16 | 19.45 | 53.87 58.82 66.09 | 38.47 54.52 64.19 |
| wal | 10 | earlier | passed | 23.59 | 14.43 | 38.47 54.52 64.19 | 35.51 52.74 63.32 |
| wal | 10 | head | passed | 30.50 | 15.34 | 41.07 53.61 63.57 | 44.09 53.20 63.08 |
| daemon | 1 | earlier | passed | 31.86 | 23.97 | 44.09 53.20 63.08 | 48.51 53.55 62.87 |
| daemon | 1 | head | passed | 40.16 | 18.48 | 48.51 53.55 62.87 | 46.38 52.17 61.92 |
| daemon | 2 | earlier | passed | 21.41 | 14.00 | 46.38 52.17 61.92 | 47.57 51.99 61.58 |
| daemon | 2 | head | passed | 33.59 | 13.13 | 47.57 51.99 61.58 | 33.61 48.22 59.88 |
| daemon | 3 | earlier | passed | 19.12 | 14.02 | 33.61 48.22 59.88 | 26.21 45.58 58.66 |
| daemon | 3 | head | passed | 31.69 | 22.63 | 26.21 45.58 58.66 | 32.39 44.67 57.78 |
| daemon | 4 | earlier | passed | 23.51 | 16.27 | 32.39 44.67 57.78 | 34.77 44.28 57.33 |
| daemon | 4 | head | passed | 31.69 | 20.63 | 34.77 44.28 57.33 | 35.80 43.49 56.52 |
| daemon | 5 | earlier | passed | 23.87 | 13.42 | 35.80 43.49 56.52 | 40.42 43.90 56.28 |
| daemon | 5 | head | passed | 18.51 | 11.81 | 40.42 43.90 56.28 | 42.04 44.11 56.14 |


**Answer:** WAL: earlier **9 passed, 1 failed**, head **10 passed**; logging: **5 passed on each tree**. The only failure was the earlier tree's separate commit-latency assertion (`worst = 2.1944 s`, required `< 2 s`), not its WAL-size assertion. Neither tree reproduced the WAL-size failure or empty-daemon-log failure. There is no evidence here that this PR causes either failure, and head did not fail more often. Both tests and their holding helpers are unchanged and are left alone.

The round-2 68.79/60.12/16.17-second single-run observations are historical, uncontrolled measurements, not a demonstrated close-version effect. This comparison's pytest times were 14.43–87.21 s earlier and 8.71–46.37 s at head; it does not reproduce that timing split. The WAL assertion precedes the changed `Store.close()` body. Two separate, uncounted diagnostics wrapped the helper's existing wait without changing its 30-second timeout or assertions. Both passed: earlier wait **13.284944 s** (pytest 22.79 s, command wall 33.28 s, load 40.82/43.78/55.88 → 49.43/45.40/56.04), head wait **11.549116 s** (pytest 22.95 s, wall 30.86 s, load 49.43/45.40/56.04 → 37.01/42.92/54.78). Each observed a 64,383,272-byte WAL with `release=False` and one reader in use, followed by normal release and shrinkage to 262,144 bytes. The reported historical 30.173-second expiry was not reproduced, and its removed scratch cannot be independently checked. These results supersede any causal inference from the round-2 one-off timings.

Step-1 commands (with `V` the permitted attempt directory's `comparison-r3` child):

```sh
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
export UV_CACHE_DIR="$TMPDIR/uv-cache"
git -c core.fsmonitor=false archive b7500b817 --format=tar --output "$V/earlier.tar"
tar -xf "$V/earlier.tar" -C "$V/earlier"
uv --cache-dir "$V/uv-cache" sync --group dev
# Sync also succeeded in the extracted earlier tree.
# Serial earlier/head loops, fresh --basetemp for each invocation:
uv --cache-dir "$V/uv-cache" run --no-sync pytest -q \
  tests/unit/test_store_readers.py::test_a_long_snapshot_holds_the_wal_back_without_holding_a_commit_and_it_shrinks_after \
  --basetemp "$V/wal-<pair>-<tree>"
uv --cache-dir "$V/uv-cache" run --no-sync pytest -q \
  tests/unit/test_daemon_log_sites.py::test_under_daemon_start_an_uncaught_exception_reaches_daemon_log_scrubbed \
  --basetemp "$V/daemon-<pair>-<tree>"
# The two uncounted WAL diagnostics add -p comparison_observe.
```

Python 3.14.7, pytest 9.1.1. Counted total: **29 passed, 1 failed**, 733.54 pytest seconds / 1157.55 command-wall seconds; diagnostics: **2 passed**, 45.74 pytest seconds / 64.13 wall seconds. Raw logs/JSON receipts are in `V`. Deviation: extraction used a tar-file intermediary rather than a pipe; the first 45-second archive timed out, the 180-second retry succeeded, and a redundant head archive was interrupted. No worktree was added, no tests ran in parallel, and no production file or existing test changed in this step.


### Step 2 — reproduction before production changes

`test_a_child_launched_after_close_finishes_cannot_take_its_first_lock[intact-root|recreated-root]` holds the starter on an event immediately before its real `Popen`, spends the real `CATALOG_HANDOFF_WAIT_S` through full service close, then releases it. The wrapper withholds `spawn_refresh`'s return until the real child exits (60-second bound). Both assertions failed against unchanged `e50cf8996` production code: **2 failed, 53 deselected, 27.69 s**. Both children exited **0** and wrote **`catalog.json` and `catalog-cache.json` after close finished**. The recreated root also received `catalog.lock`; the intact root's parent probe had already made that file before close. The finding is **proven in both rows**. Every child was reaped and every starter joined.

Command: `uv --cache-dir "$V/uv-cache" run --no-sync pytest -q tests/unit/test_conversation_catalog_lifecycle.py -k child_launched_after_close --basetemp "$A/r3-red" --junitxml="$A/r3-red.xml"`, with the required TMPDIR/UV_CACHE_DIR exports. `A` is the permitted attempt directory; log: `A/r3-red.log`.



### Step 3 — change

- **(a)** `begin_close()` takes an in-flight start's writer under `_catalog_lock`, leaves `(read, None)`, then closes that writer outside the lock before returning. `_finish_catalog_close()` accepts the absent writer; the starter closes the remaining read end exactly once after inheritance is finished or no child will start.
- **(b)** The starter makes its fence outside the lock, assigns it under the lock and rechecks `_closed` in that same section before probing or spawning. A close before the fence exists now starts nothing and the starter's handoff disposes of the fence it made.
- **(c)** `refresh_running()` uses `transcripts.regular_fd()`: its open is nonblocking, checks the opened descriptor is regular, and never uses `O_CREAT`. An absent lock in an existing root is `False`; a missing root is `None`, preventing spawn. The run itself still uses the unchanged creating `transcripts.lock_fd()`. Probe, listing, declined-start and spawn-error tests check that parent paths create no lock. The replacement-root test now expects an empty root; the closed-owner child test now also exercises the parent spawn probe.
- **(d)** C-30.1, C-5.8a, their dated change log and the round-2 account above are corrected. The named sum remains **`max(1 + 10, 2 + 2 + 15 + 2, 2, 5) + 2 + 5 = 28 s`**, at least **2 s under `STOP_GRACE_S = 30 s`**. The real-close fake-clock test now includes two readers sharing the same 2 s deadline (receipts 2 s, then 0 s).

The remaining race is exactly this: **a run that read its fence open before `begin_close()` returned can still create `catalog.lock` once**. The sweep and publishes also act by path after their Owner checks, as already documented; a prior successful check does not make its following path operation atomic with close or root replacement. This change does not remove those check-then-act windows.

Compatibility/budget command: `uv run --no-sync pytest -q tests/unit/test_conversation_service.py tests/unit/test_stop_watchdog.py -k 'close_at_each_catalog_start_boundary or close_bounds_the_start_handoff_and_the_late_starter_reaps or the_grace_outlasts_probe_containment'` (Darwin TMPDIR, writable cache override, fresh `--basetemp`; cache-variable deviation below): **9 passed, 138 deselected, 13.72 s**.


Step-3 probe/real-child command: `uv run --no-sync pytest -q tests/unit/test_conversation_catalog.py tests/unit/test_conversation_catalog_lifecycle.py -k 'child_launched_after_close or parent_probe or parent_probes or listing_and_failed or root_is_replaced or started_after_its_owner'`, same exports/cache override, new `A/r3-step3` base: **10 passed, 75 deselected, 21.33 s**. Both formerly writing late children now exit **OWNER_GONE (3)**, create no lock and leave their root snapshot unchanged.


### Step 4 — invariants and fixed rows

Test names below are in `tests/unit/test_conversation_catalog_lifecycle.py` unless another file is given.

| Invariant | Test |
| --- | --- |
| 1. After `begin_close()` returns, no run the service starts later passes its first Owner check. | `test_a_child_launched_after_close_finishes_cannot_take_its_first_lock`, both real-child roots, plus `test_concurrent_close_claims_finish_the_same_writer_revocation`; `test_close_at_any_catalog_starter_step_preserves_fence_ownership`, actual `Owner` against a valid seeded lock in each draw. |
| 2. Parent probes create no file in an existing root, and a missing root stays missing. | `test_parent_probes_create_nothing` (existing/missing/recreated) and `test_a_parent_probe_of_a_missing_root_never_spawns` in `test_conversation_catalog.py`; `test_listing_and_failed_starts_leave_no_parent_lock` covers listing and both start-return probe paths. |
| 3. Each fence descriptor closes exactly once. | Generated starter property, `test_close_before_fence_assignment_starts_nothing_and_closes_each_end`, `test_a_spawn_oserror_during_close_closes_each_fence_end_once`, and service boundary tests. |
| 4. The read end's number stays held until inheritance finishes or no child will start. | Generated property checks its original pipe identity and refuses an early read close; real late-Popen rows inherit a valid pipe after close. |
| 5. Every spawned child is tracked, still-running children are signalled, and each is reaped, including a handoff after close finished. | `test_a_real_run_is_reaped_at_each_catalog_handoff` records the tracked run at signal and an explicit service reap receipt; late rows require that receipt from the starter thread. The reproduction's held Popen wrapper waits/reaps its child before handoff; the service then tracks and quietly clears that exited Popen without an unnecessary signal. The real-handoff rows separately prove service/starter reaping. |
| 6. The named stop sum remains at least 2 s under `STOP_GRACE_S`. | Both rows of `test_c5_8a_the_grace_outlasts_probe_containment_and_the_backstops_outlast_the_grace` in `test_stop_watchdog.py`: 28 s, including two readers sharing one deadline. |

| Close arrives | Fixed test and result |
| --- | --- |
| Before the fence exists | `test_close_before_fence_assignment_starts_nothing_and_closes_each_end[before-fence]`: no spawn, both eventual descriptors closed once. |
| After the fence, before spawn | Same test `[after-fence]`: closed-state recheck prevents spawn and disposes of both descriptors. |
| During spawn, held before real Popen | `test_a_child_launched_after_close_finishes_cannot_take_its_first_lock[intact-root\|recreated-root]`: exit OWNER_GONE, no lock and identical root snapshot. |
| After spawn, before handoff | `test_a_real_run_is_reaped_at_each_catalog_handoff[before-handoff\|late-handoff]`, real TERM and missed-TERM variants: tracked at signal and reaped; missed TERM independently reaches a later Owner check and exits OWNER_GONE. Late rows finish full service close before handing back the child and require the starter to reap. |
| After handoff | Same real-child test `[after-handoff]`, both signal variants; the existing path remains in use. |
| Spawn raises OSError during close | `test_a_spawn_oserror_during_close_closes_each_fence_end_once`: full close finishes while spawn is held, writer closes once immediately, read once after the raising handoff, no lock. |

The new Hypothesis property draws six close points and 1–3 repeated close claims, uses a fake spawn recording real descriptor identities and actual `Owner` checks, and exhausts **18 passing examples, 0 failing, 0 invalid**. Per-example cleanup keeps mutation failures in the call phase.

Step-4 commands (same required exports/cache override and fresh bases): `pytest -q tests/unit/test_conversation_catalog_lifecycle.py -k 'close_before_fence_assignment or spawn_oserror_during_close or real_run_is_reaped'`: **9 passed, 58 deselected, 13.51 s**; `pytest -q tests/unit/test_conversation_catalog_lifecycle.py -k close_at_any_catalog_starter_step --hypothesis-show-statistics`: **1 passed, 57 deselected, 5.32 s**. The property run collected before the nine fixed rows were appended; the final run below collects them together.


Independent review of step 4 found a concurrent-close variation: one claimant could detach the writer and pause before closing it while a second claimant returned with that writer still open. To satisfy invariant 1 for every close return, `_catalog_revoke_lock` serializes only the state claim and writer close. It holds no spawn, read-end cleanup, logging, signal send or child wait; no bounded wait was added, and the qualified 28 s sum is unchanged. `test_concurrent_close_claims_finish_the_same_writer_revocation` pauses the first writer close, checks the second claimant cannot return yet, then launches the real child after full close: **1 passed, 10.73 s**, exit OWNER_GONE, no lock and unchanged snapshot. Its first run had **1 harness failure, 3.74 s** because the test expected an empty root despite the service's preexisting `conversations.sqlite3`; the corrected test compares snapshots. This extra serialization is a necessary extension of step 3(a) discovered while checking the requested invariant, not an extra shutdown deadline.

After serialization and cleanup strengthening, the fixed rows, generated ownership property, service boundaries and both real-close budgets passed together: **19 passed, 196 deselected, 15.32 s** (`pytest -q` over lifecycle/service/watchdog with the matching name selection, fresh `A/r3-serialization` base). The real-handoff test restores raw signalling and explicitly kills/reaps owned children during failure cleanup so a failed tracking assertion cannot suppress teardown containment.


### Step 5 — executed mutants

Each variant ran in its own temporary copy of the changed source/tests and project files, without adding a worktree or editing workspace production. Before pytest, resolved imported module paths were checked against that copy; real catalog children used that copy's package-root environment. Pristine copied baselines passed **10 cases in 37.22 s** (including 18 property draws) and **5 cases in 12.60 s**. All runner/test processes exited, and real child cleanup joined/waited every child and thread.

`property` means `test_close_at_any_catalog_starter_step_preserves_fence_ownership`; `budget` means `test_c5_8a_the_grace_outlasts_probe_containment_and_the_backstops_outlast_the_grace`; `late` means `test_close_bounds_the_start_handoff_and_the_late_starter_reaps`.

| Mutant | Killing test and causal failure | Call failures | Pytest s |
| --- | --- | ---: | ---: |
| Keep writer open during a start: skip detaching/revoking the starting fence. | `test_a_child_launched_after_close_finishes_cannot_take_its_first_lock`, both real-child roots: exit 0 and lock/JSON writes after close. | 2 | 15.89 |
| Close read end too in begin_close; remove it from tuple to avoid an unrelated double-close. | `property`: read number released before inheritance, shrunk to before-inheritance/one close. | 1 | 27.57 |
| Remove only the starter's second locked `_closed` check. | `property`: actual Owner admits first check after close, before-allocation/one close. | 1 | 27.65 |
| Restore `transcripts.lock_fd()`/O_CREAT in refresh_running. | `test_parent_probes_create_nothing` existing/recreated rows and both `test_listing_and_failed_starts_leave_no_parent_lock` rows: new lock appears. Missing-root probe/spawn controls pass. | 4 | 16.40 |
| Add unmodelled `threading.Event().wait(timeout=0.125)` at Daemon.close start. | `budget`, both rows: `unmodelled bounded wait: Event.wait`. | 2 | 17.03 |
| Remove starter's `_stop_catalog(handoff_owner=True)`. | `late`, elapsed 0/0.5/1: run remains in `_catalog_proc` after the late starter returns. | 3 | 13.80 |
| Remove Store.close reader deadline cap, using fresh `CLOSE_WAIT_S`. | `budget`, both rows: **33 s**, not 28. | 2 | 13.44 |
| Give each connection reader fresh READER_JOIN_S instead of shared deadline. | `budget`, both rows: **30 s**, not 28. Second interpretation of the requested reader-cap mutant. | 2 | 4.13 |
| Extra: replace revoke serialization with `contextlib.nullcontext()`. | `test_concurrent_close_claims_finish_the_same_writer_revocation`: a concurrent close returns with writer still open. | 1 | 13.87 |

**All seven requested mutants killed, across eight variants; the additional concurrent-close negative control was also killed: 9/9 variants, 18 expected call-phase failures, 2 passing controls, zero setup/teardown errors.** No mutant survives in the workspace. The fence group verified unchanged workspace source/test bytes against its pristine copy after running; only a later module-docstring wrap changed.

Mutation commands, normalized to permitted attempt directory `A`, ran from each copy with `UV_PROJECT_ENVIRONMENT=<workspace>/.venv` and `PYTHONPATH=<copy>`:

```sh
uv --cache-dir "$A/comparison-r3/uv-cache" run --no-sync python -c <assert imported source paths are under copy>
uv --cache-dir "$A/comparison-r3/uv-cache" run --no-sync pytest -q <table node IDs> \
  --basetemp "$A/<mutation-group>/<fresh-base>" \
  --junitxml "$A/<mutation-group>/<variant>.xml"
# Fence/property variants additionally use --hypothesis-show-statistics.
```

Raw mutation replacements, import receipts, commands, logs and XML remain in `A/fence-mutants-r3` and `A/budget-mutants`; essential failure receipts are preserved above. The fence group used the exact prescribed separate TMPDIR/UV_CACHE_DIR exports. Cache-only deviation: the delegated budget mutation group and its focused compatibility run set `UV_CACHE_DIR` directly to the permitted fallback cache instead of leaving it at `$TMPDIR/uv-cache`; they still passed `--cache-dir` explicitly and used Darwin TMPDIR/fresh test roots. The final nine-file command uses the prescribed exports exactly.


### Final validation and delivery

The required nine files passed together on Python 3.14.7 with four xdist workers: **402 passed, 7 skipped, 268.72 s**. The seven skips are the existing sandboxed macOS process/boot-inspection cases. No catalog, service, stop-budget, daemon-log or contract guard failed.

```sh
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
export UV_CACHE_DIR="$TMPDIR/uv-cache"
uv --cache-dir "$A/comparison-r3/uv-cache" run --no-sync \
  --with 'pytest-xdist>=3.6' pytest -q -n 4 --timeout=300 \
  tests/unit/test_conversation_catalog.py \
  tests/unit/test_conversation_catalog_lifecycle.py \
  tests/unit/test_conversation_service.py \
  tests/unit/test_stop_watchdog.py \
  tests/unit/test_daemon_verbs.py \
  tests/unit/test_daemon_log_sites.py \
  tests/fake/test_state_contract.py \
  tests/unit/test_test_module_names.py \
  tests/unit/test_invariants_index.py \
  --basetemp "$A/r3-final-nine" --junitxml "$A/r3-final-nine.xml"
git diff --check
```

Final log/XML: `A/r3-final-nine.{log,xml}`. `git diff --check` passed. All child/test processes and starter/closer threads finished; no background work remains. No new event/record key or refusal was introduced, no dependency manifest changed, no live state was read or modified, and the whole suite remains for CI.

**Git did not let me commit.** Named-file staging after steps 1, 2, 3 and 4, the concurrent-close correction, and mutation results was refused because Git cannot create the shared worktree's `index.lock` (`Operation not permitted`). The final attempt to stage the same eight named files was also refused: seven named-file staging attempts in total, no commits. No alternate metadata, branch, worktree, history rewrite or push was used. All eight files remain in the assigned workspace for hub collection. Deviations are the tar-file archive intermediary, the disclosed cache-variable fallback, and the necessary concurrent-close serialization plus its extra negative control. Work and reporting finished at 06:05 UTC on 2026-11-07, within three hours of the 05:07 UTC start.
