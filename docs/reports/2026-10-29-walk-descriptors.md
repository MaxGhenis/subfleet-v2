# D-WALK-FD1: retention filesystem descriptor ownership

The release at `bc108d3ad05f` opens a child in `walk`, then lists it while
constructing the stack entry. A listing failure leaves that descriptor outside
the stack's `finally`, so repeated sizing failures accumulate descriptors in
the daemon (`AttemptTemp.size` catches the error).

The new example test repeats the hub's reproduction: three `dN/x/f` branches,
a soft descriptor bound of 256 (raised only if pytest's existing descriptors
need more headroom), filler opens up to `EMFILE`, and N=1,2,3,4 free slots.
The bound is restored and all fillers are closed in `finally`. The hub's
recorded baseline was +1 descriptor for each of N=1 and N=2, and zero for
N=3 and N=4. Our red/green results are below. No installed release or live
state was accessed by our reproduction.

Tests cover normal completion, injected listing/stat/check failures, the named
refusals, and early generator closure, while leaving the borrowed `root_fd`
open. Review r1's interrupted cleanup cases close the remaining owned
descriptors. Fix r2 centralizes explicit error and finally cleanup; fix r3
uses the operation's explicit exit exception and owns each walk iterator.
The invariant below is checked for the exercised paths and instruction windows. Hypothesis varies
small directory/file/symlink trees and the k-th listing
or stat, verifies exact descriptor-set equality and injected exception identity,
and varies early close positions. It runs 100 failure examples and 60 close
examples, plus explicit examples. Tests close detected leaks even when red,
so later examples and pytest retain their original descriptors.

## Invariant

Achieved in the fix r3 test scenarios, with coverage scoped to those scenarios.
The [r3 report](2026-10-29-walk-descriptors-r3.md) names the remaining
pure-Python marker-to-close-call instruction window and other coverage boundaries.

Each owned descriptor receives at most one close attempt; the borrowed root stays
open. With no exception in flight, cleanup tries every owned close, then raises
the first close error. An exception in flight propagates unchanged and close
errors do not replace it, except that cleanup cancellation overrides an in-flight
`Exception` or `GeneratorExit`, with that exception as its `__context__`. An
in-flight cancellation wins over every close error. Cancellation means a
`BaseException` that is neither an `Exception` nor `GeneratorExit`, including
`KeyboardInterrupt`, `SystemExit`, and custom `BaseException` subclasses.

## Decisions and descriptor audit

1. `walk` lists each child before the stack owns it, closes it on any exception
   raised before the stack holds it, and re-raises. An exception immediately
   after registration leaves cleanup to the stack, without a second close.
   Existing exception translations and `TreeError.reason` values are preserved.
2. All descriptor opens in `retention_fs.py` were read. Original line numbers:

| Site | Ownership result |
| --- | --- |
| `open_dir`, 162 | Close on failed `fstat`/signature validation, including non-`Exception` failures; mismatch still raises `swapped`. |
| `walk`, 207 | Close an unowned child if listing/stack handoff fails; after append, only the stack closes it. |
| `_layout`, 557 | Allocate the empty containers before opening; enter an owning cleanup context immediately after the open. |
| `RegenerableWalk._in_repository`, 1020 | Registration failure closes only an unowned descriptor; cleanup attempts every registered close once under the invariant above. |
| `_open_path`, 1043 | Failed later opens close the current intermediate descriptor. The new child takes ownership before closing the previous descriptor, which receives one close attempt. Failed handoff cleanup closes only the child under the invariant above. The borrowed starting descriptor stays open. |
| `_remove_children`, 1167 | Enters an owning cleanup context immediately; recursive work is protected and cleanup uses `_close_all` with its explicit exit exception. |
| `Reclaim._one`, 1253 | Enters an owning cleanup context after the open's exception handler; mode changes and recursion are protected and cleanup uses `_close_all` with its explicit exit exception. |
| `Reclaim._conflict_dir`, 1304/1311 | Mkdir, component preparation, and later-open failures are guarded. The new child takes ownership before the previous descriptor receives one close attempt. Cleanup closes only the child; cancellation from that close propagates through `_set_aside_fd`. |
| Remaining opens: 123, 231, 305, 471, 482, 605, 640, 687, 709, 717, 917, 932, 1053, 1077, 1080, 1104, 1118, 1131, 1152, 1213, 1342 | `still_archived`, `tree_bytes`, `clone_or_copy`, `_package_or_scope` (both opens), `bytecode`, `installed_files`, `record_complete`, `_site_packages` (both opens), `_rule_for`, `_start`, `_small`, `_marker` (both opens), `sync_path`, `write_atomic`, `read_regular`, `remove_own_tree`, `Reclaim.run`, and `_set_aside` protect subsequent work with owning cleanup contexts; those closes use `_close_all` with the operation's explicit exit exception. The named walk consumers also explicitly own their iterators. |

3. The invariant includes ordinary exceptions and cancellation-like
   `BaseException` injection, root descriptor preservation, and deterministic
   checks of the named `TreeError` exits.
4. Added `tests/unit/test_retention_walk_descriptors.py`. The invariant index
   guards pin exactly 220 legacy ledger rows and do not require registration
   of this new regression, so that ledger is unchanged.
5. In the original build, both requested mutants ran against the new test
   file only; no mutant sweep was run. The fixed source was restored afterward.

## Execution evidence

All runs used the locked dev environment, Python 3.14.7, serial pytest, disabled
bytecode and pytest's cache provider, isolated TMPDIR and Hypothesis storage,
and the shared hold wrapper. A scratch-only evidence plugin recorded UTC start,
load averages (1/5/15 minutes), and per-file results. It redirected failed-test
retention into scratch to avoid pruning unrelated job artifacts. Successful
pytest temp roots were removed during the required regression run.

Fix/test commit: `cf155efd14850069822b2c369766d22f985468f6`, based on `bc108d3ad05f87b14fc1fb34e258e93dbba41765`. All holds were admitted; none ran outside the lock. R queued at 10:50:42 UTC, within the first hour. All holds started within three hours.

| Run | Pytest start UTC (2026-10-29) | Load 1/5/15 | Result | Test duration |
| --- | --- | --- | --- | --- |
| R | 11:14:26 | 38.36/38.88/32.67 | 9 passed, 11 failed | 12.05s |
| G1 | 11:15:42 | 28.95/35.87/32.05 | 687 passed, 6 failed, 1 deselected; interrupted before 63 cases completed (process exit 130) | 1117.48s |
| G2 | 11:39:24 | 22.17/27.62/28.24 | 69 passed, 1 teardown error | 523.36s |
| M1 | 11:51:11 | 24.50/19.09/21.28 | 16 passed, 4 failed | 7.73s |
| M2 | 11:56:18 | 23.94/24.66/23.33 | 9 passed, 11 failed | 8.72s |
| G3 | 12:01:44 | 17.13/19.32/21.23 | 21 passed | 13.84s |

R used the unchanged release file. G1 tested the fix plus the required file set. Its complete descriptor-file result was 20 passed. G2 finished the uncompleted files and retried the six archive cases. G3 checked the restored fix and retried the teardown case with default temp retention. G1 was stopped with the tool session’s Ctrl-C before the 20-minute hold bound; direct SIGINT from another command was sandbox-denied. Its plugin recorded exit 0 before interruption reporting, so that metadata is not treated as a successful run.

Per-file results below reference those run start times and loads. P = passed, F = failed, E = teardown error. There are 756 selected cases; all ultimately passed after the documented runner adjustments. No test or application bound was loosened.

| File under `tests/` | G1 | G2 | G3 |
| --- | --- | --- | --- |
| `fake/test_attempt_tmpdir_daemon.py` | 24P | — | — |
| `fake/test_attempt_tmpdir_leaks_daemon.py` | 8P | — | — |
| `fake/test_state_contract.py` | 37P | — | — |
| `unit/test_attempt_tmpdir_guardian.py` | 2P | — | — |
| `unit/test_attempt_tmpdir_leaks.py` | 197P | — | — |
| `unit/test_attempt_tmpdir_lifecycle.py` | 35P | — | — |
| `unit/test_attempt_tmpdir_mutations.py` | 85P | — | — |
| `unit/test_attempt_tmpdir_retention.py` | 6P | — | — |
| `unit/test_attempt_tmpdir_sandboxes.py` | 12P | — | — |
| `unit/test_host_push_metadata.py` | 33P | — | — |
| `unit/test_invariants_index.py` | 25P | — | — |
| `unit/test_retention_81_notes.py` | 38P | — | — |
| `unit/test_retention_archive.py` | 102P, 6F | 6P | — |
| `unit/test_retention_readback.py` | 37P | — | — |
| `unit/test_retention_regenerable.py` | 26P | — | — |
| `unit/test_retention_regenerable_properties.py` | — | 1P | — |
| `unit/test_retention_sweep_race.py` | — | 13P | — |
| `unit/test_retention_turns.py` | — | 7P | — |
| `unit/test_retention_walk_descriptors.py` | 20P | — | 20P |
| `unit/test_review130_pass.py` | — | 21P | — |
| `unit/test_test_module_names.py` | — | 1P | — |
| `unit/test_timers_retention.py` | — | 20P, 1E | 1P |

G2 also captured per-file first setup-report UTC time and contemporaneous load:

| File | First report UTC | Load 1/5/15 |
| --- | --- | --- |
| `unit/test_retention_archive.py` | 11:39:34 | 20.40/27.07/28.04 |
| `unit/test_retention_regenerable_properties.py` | 11:40:04 | 15.21/25.20/27.32 |
| `unit/test_retention_sweep_race.py` | 11:42:52 | 13.59/19.39/24.48 |
| `unit/test_retention_turns.py` | 11:47:31 | 11.85/15.03/21.04 |
| `unit/test_review130_pass.py` | 11:47:35 | 11.62/14.93/20.96 |
| `unit/test_test_module_names.py` | 11:47:55 | 11.48/14.69/20.74 |
| `unit/test_timers_retention.py` | 11:47:55 | 11.48/14.69/20.74 |

## Mutants

| Variant against the new descriptor file only | Passed | Failed | Evidence |
| --- | --- | --- | --- |
| Fixed source (G1 and G3) | 20 | 0 | 20 committed build tests pass, including early close and root preservation. Review r1's additional cleanup and handoff cases now pass; see the follow-up report. |
| M1: remove only the walk child close, retaining the reordering and other fixes | 16 | 4 | N=1, N=2, injected listing failure, and unreadable child leak. |
| M2: restore the entire release file | 9 | 11 | Same failures as R, including validation and path-descent leaks. |

The fixed file was restored after each mutant and compared byte-for-byte with
the committed version. G3 passed after both mutants. No hub decision required
changing.

## Runner adjustments and exclusions

The sandbox refused the requested `$(getconf DARWIN_USER_TEMP_DIR)sf-walkfd`
folder, so the environment, metadata and initial tests used `/private/tmp/sf-walkfd`.
The literal `tmp` path component triggers `retention_git.scratch_reason` even
when the archive fixtures override temporary roots. This caused six G1 archive
trust/omission failures: round trip, local-only blob, local remote, alternates,
shallow, and survey omission accounting. All six passed in G2 under the allowed
per-job Darwin temp root’s `sf-walkfd` child. Production code was unchanged.

The optional `tmp_path_retention_policy=failed` setting caused G2's teardown
error in `test_an_entry_deletion_cannot_remove_is_set_aside_not_left_half_deleted`:
pytest's eager cleanup called its patched `os.unlink` after the fixture's
SQLite connection had closed. G3 used default temp retention and passed the
case, including teardown. No fixture or application source was changed.

The archive file's `test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` was
deselected because this lane forbids live host process inspection. The full
suite and `tests/frontend/test_core_live.py` were not run; the lane rules leave
them to CI. Every other requested test completed. Existing Git fixture traffic
used test-owned scratch repositories; no caller repository fetch, merge or push,
real CLI executable, provider, daemon or live store was used.

Shared Git metadata refused branch writes. The fix/test commit and this report
were committed on `fix/walk-fd-leak-217` using isolated metadata with read-only
access to the base objects. Delivery is the verified incremental bundle
`docs/reports/2026-10-29-walk-descriptors.bundle`, containing that branch from
the stated base. Only named files were staged, and both commit messages carry
the requested coauthor trailer.

Scratch samples stayed below 30 MiB combined, below the 200 MB cap. Both owned
scratch roots are removed with the lane rules' guarded cleanup after final
bundle verification. No test or setup process is kept running.

## Review r1 follow-up

The execution tables above describe the original build. Review r1 found two
additional gaps at `d3a48269a`: descent handoff leaked a new child when closing
the previous descriptor raised, and interrupted cleanup left later descriptors
open. Fix r1 addressed both, but its handoff retried the previous descriptor's
number after a raising close, and its drain replaced active exceptions.

The [r1 execution report](2026-10-29-walk-descriptors-r1.md) and
[r1 mutant table](2026-10-29-walk-descriptors-r1-mutants.md) retain that round's
historical runs. Review r2 (Claude Opus 5.5, lane job
`20261029-155526-pr279-review-r2`) requested changes at `a98ee557a`: P2-3 and
P3-1–P3-4. Its regression file was committed as `9629969c4`.

Fix round r2 removes handoff retries, respects registration ownership after
append, and centralizes explicit error and finally cleanup through `_close_all`.
Normal walk completion and successful descent handoffs still call `os.close`
directly. Review r3 found P2-4, P2-5, P3-5 and P3-6 at that head.
The `raise_at` docstring now describes Python 3.12's first traced frame correctly;
`sys.monitoring` reaches every instruction on both target Pythons. The
[r2 execution report](2026-10-29-walk-descriptors-r2.md) records the red/green
runs, precedence examples, release differentials and all thirteen named mutants.


## Review r3 and fix round r3

Review r3 requested changes at `03d9270b2`. At the merged starting head
`0a433cfe1`, its file has nine failures and one passing control on each Python
when strict expected-failure marks are bypassed. Fix r3 passes all ten without
marks: cleanup receives its own operation's exit exception, `AttemptTemp.size`
uses the same root owner, the pop transition retains ownership until its close
attempt marker, and all ten `walk` consumers explicitly close their iterators.
Handled caller exceptions no longer suppress a normal cleanup error or cancellation.

The new Hypothesis property generates pending/handled exceptions and one to five
close outcomes, asserting each owned descriptor's single close attempt and the
specified exception identity and context. The AST guard rejects ambient exception
getters in both cleanup modules. Instruction tests cover pop return through the
attempt marker and close return; the marker-executed-to-close-call window and
interruptions inside native `close` remain outside the proven coverage.
Helper construction/entry and normal context-exit setup instruction failures are
also untested; an allocation failure before the owner is entered can leave the
just-acquired resource unowned. The pre-existing `RegenerableWalk` construction
before root ownership in `_Builder._walk` and `_sample_walk_once` remains outside
this round's exercised failures.
The [r3 report](2026-10-29-walk-descriptors-r3.md) records the final runs,
mutants, consumer audit, delivery bundle and coverage limits. Historical tables
above retain their original run counts.
