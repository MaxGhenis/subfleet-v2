# D-WALK-FD1: review r2 fixes and execution evidence

Base: `b20cf84add6aa8c76e411bcca83f6f0eb9867b8d`. Work began 2026-10-30 at
03:02:34 UTC. Production/test head before the text commit: `5aa93271d807ceb69c664b4317b10ec321288c69`.
The first red hold started at 03:05:52 UTC, within the first hour.

## Decisions built

1. P2-3 (`3136ea98d`): both descent loops transfer ownership using
   `parent, fd = fd, open_dir(part, dir_fd=fd)` before attempting the parent
   close once. `_open_path` retains its `parent != dir_fd` guard. Outer cleanup
   owns only the child; no raising close is retried.
2. P3-1 (`1b6989c67`): walk and repository registration handlers close only a
   descriptor their owning collection does not already hold. Post-append
   interrupts now propagate unchanged with one close per owned descriptor.
3. P3-2/P3-3 (`6594392bd`): explicit error and finally cleanup in
   `retention_fs.py` uses `_close_all`, including all 27 pre-existing
   single-descriptor finally blocks, validation, registration handlers, descent
   handlers and both drains. Normal walk completion and successful descent
   handoffs still called `os.close` directly.
   The [main invariant](2026-10-29-walk-descriptors.md#invariant) defines precedence.
   The named single-failure differentials retain the original exception type,
   TreeError code, errno and message for their executed inputs. A natural
   GeneratorExit plus a failed close is the deliberate two-exception case below.
   Review r3's handled-caller, root-close, pop and finalization counterexamples
   expose the remaining gaps at this historical head.
4. P3-4: the review file's docstring now states that settrace with opcode
   tracing enabled in the call event misses every opcode in the first frame
   traced in a Python 3.12 process. sys.monitoring INSTRUCTION events reach every
   instruction on both target versions. No historical commit message was rewritten.
5. Tests (`8bf57f292`, `9e3746b6a`, `5aa93271d`): the three visible-effect examples,
   a 42-case exception matrix, a multi-error drain example, GeneratorExit closure,
   and an actual AttemptTemp.size cancellation example are covered by 48 tests.
   The review r2 file differs from the base only by its two removed xfail
   decorators (six handoff and two registration cases), verified byte-for-byte.

No functional hub decision required changing.

## Changed expectations and probe corrections

Decision 3 changes no existing expectation in
`test_retention_descriptor_review.py` or `test_retention_walk_descriptors.py`.
For P2-3 only, the three r1 cases
`test_descent_close_failure_owns_new_child[False-path]`, `[False-conflict]`,
and `[False-set-aside]` now require exactly one parent-close attempt and a closed
child; the test itself releases the synthetic stranded parent. A real raising
close may already have released its descriptor number, so retrying is unsafe.

The scratch assertion in `test_33_walk_plans` initially required release parity
for close + OSError. Counterexample: advance a four-level walk five times,
then `iterator.close()` with the first owned close releasing and raising EIO.
Release raises OSError; the new invariant preserves GeneratorExit and close
returns normally after draining. The smallest correction is to that probe
expectation, pinned by `test_walk_close_preserves_generator_exit_over_close_oserror`;
production behavior already followed the hub decision. The other corrected
probe assumption was that a third-close interrupt could fire in a too-deep
walk which opened only two children. Expected cancellations now consider
only close indices actually reached. All exploratory failures are retained below.

## Environment and runs

Python 3.14.7 and 3.12.14 used separate locked dev venvs, initialized under the
environment lock. All test invocations used `uv run --no-sync`, serial pytest,
one pytest per shared-lock hold, at most one worker per pytest, removed
SUBFLEET_RETENTION_DORMANT/PYTHONPATH, disabled bytecode and pytest cache,
and task-owned TMPDIR and Hypothesis storage. The evidence runner redirects
failed_keep into scratch before conftest pruning. The wrapper's sysctl read
was sandbox-denied; all recorded loads use os.getloadavg(). No hold ran
outside the lock. All 60 holds started within the first hour. Every pytest completed below ten minutes (longest
236.53s); observed parent-process peak RSS was 153.5 MiB.

Times below are 2026-10-30 UTC; loads are 1/5/15-minute averages at the first
test in each file. Durations cover the whole hold. R2 = review r2 file,
Q = review r1 file, W = original descriptor file, C = new cleanup file.
P/F mean passed/failed. The complete arguments and failing node IDs are in
[the evidence JSON](2026-10-29-walk-descriptors-r2-evidence.json).

| Hold (Python) | File under tests/ or scratch selection | First test UTC | Load | Result | Seconds |
| --- | --- | --- | --- | --- | ---: |
| red314 (3.14.7) | `unit/R2` | 03:05:55 | 19.41/21.22/23.60 | 8F | 5.36 |
| red312 (3.12.14) | `unit/R2` | 03:11:47 | 32.88/27.73/25.91 | 8F | 7.68 |
| handoff (3.14.7) | `unit/R2` | 03:15:10 | 26.44/26.94/25.96 | 6P | 2.55 |
| handoff (3.14.7) | `unit/Q` | 03:15:11 | 26.44/26.94/25.96 | 6P | 2.55 |
| green314 (3.14.7) | `unit/R2` | 03:14:50 | 26.42/26.97/25.95 | 8P | 6.21 |
| green312 (3.12.14) | `unit/R2` | 03:15:05 | 26.39/26.94/25.96 | 8P | 3.35 |
| review314 (3.14.7) | `unit/Q` | 03:15:15 | 28.09/27.27/26.08 | 47P | 3.78 |
| review312 (3.12.14) | `unit/Q` | 03:15:21 | 28.88/27.45/26.15 | 47P | 3.22 |
| walk314-1 (3.14.7) | `unit/W` | 03:15:27 | 28.89/27.47/26.17 | 20P | 5.59 |
| walk314-2 (3.14.7) | `unit/W` | 03:15:36 | 27.42/27.21/26.09 | 20P | 6.84 |
| walk314-3 (3.14.7) | `unit/W` | 03:15:48 | 28.59/27.47/26.21 | 20P | 7.89 |
| walk314-4 (3.14.7) | `unit/W` | 03:16:02 | 27.74/27.33/26.17 | 20P | 7.96 |
| walk314-5 (3.14.7) | `unit/W` | 03:18:11 | 25.47/26.39/25.93 | 20P | 2.62 |
| walk312 (3.12.14) | `unit/W` | 03:18:16 | 24.87/26.25/25.89 | 20P | 2.92 |
| cleanup314 (3.14.7) | `unit/C` | 03:18:19 | 24.88/26.23/25.88 | 46P | 1.65 |
| cleanup312 (3.12.14) | `unit/C` | 03:18:22 | 24.88/26.23/25.88 | 46P | 1.53 |
| cleanup314-final (3.14.7) | `unit/C` | 03:22:25 | 20.63/23.36/24.65 | 47P | 1.64 |
| cleanup312-final (3.12.14) | `unit/C` | 03:24:32 | 20.08/22.25/24.04 | 47P | 2.73 |
| cleanup314-head (3.14.7) | `unit/C` | 03:28:47 | 20.66/21.82/23.43 | 48P | 2.35 |
| cleanup312-head (3.12.14) | `unit/C` | 03:29:02 | 18.93/21.39/23.25 | 48P | 6.26 |
| parity314 (3.14.7) | `test_parity.py` | 03:18:25 | 25.13/26.26/25.89 | 1F, 3P | 5.02 |
| parity314-fixed (3.14.7) | `test_parity.py` | 03:22:08 | 20.12/23.37/24.68 | 1F, 3P | 7.24 |
| parity312 (3.12.14) | `test_parity.py` | 03:22:17 | 21.96/23.70/24.79 | 1F, 3P | 7.21 |
| walk-plans314 (3.14.7) | `test_parity.py` | 03:24:25 | 19.48/22.17/24.02 | 1P | 1.76 |
| walk-plans312 (3.12.14) | `test_parity.py` | 03:24:29 | 20.08/22.25/24.04 | 1P | 2.11 |
| archive1 (3.14.7) | `unit/test_retention_archive.py` | 03:20:56 | 22.86/24.14/25.03 | 38P | 136.48 |
| archive2 (3.14.7) | `unit/test_retention_archive.py` | 03:23:15 | 20.09/22.98/24.45 | 70P | 236.53 |
| turns (3.14.7) | `unit/test_retention_turns.py` | 03:27:30 | 20.91/22.16/23.68 | 7P | 7.17 |
| sweep-examples (3.14.7) | `unit/test_retention_sweep_race.py` | 03:27:44 | 22.42/22.43/23.75 | 12P | 68.60 |
| sweep-explicit (3.14.7) | `unit/test_retention_sweep_race.py` | 03:28:59 | 18.93/21.39/23.25 | 1P | 42.21 |
| sweep-generated (3.14.7) | `unit/test_retention_sweep_race.py` | 03:29:43 | 15.70/20.23/22.73 | 1P | 136.84 |
| readback (3.14.7) | `unit/test_retention_readback.py` | 03:32:07 | 17.59/19.10/21.86 | 37P | 110.29 |
| regenerable (3.14.7) | `unit/test_retention_regenerable.py` | 03:34:11 | 19.55/19.40/21.59 | 26P | 99.80 |
| regenerable-properties (3.14.7) | `unit/test_retention_regenerable_properties.py` | 03:36:11 | 20.71/19.58/21.35 | 1P | 75.07 |
| timers (3.14.7) | `unit/test_timers_retention.py` | 03:37:26 | 24.65/21.13/21.79 | 20P | 6.46 |
| fake-tmpdir (3.14.7) | `fake/test_attempt_tmpdir_daemon.py` | 03:37:39 | 24.64/21.30/21.84 | 24P | 30.97 |
| fake-leaks (3.14.7) | `fake/test_attempt_tmpdir_leaks_daemon.py` | 03:38:16 | 23.34/21.41/21.86 | 8P | 6.07 |
| unit-guardian (3.14.7) | `unit/test_attempt_tmpdir_guardian.py` | 03:38:20 | 22.43/21.25/21.80 | 2P | 1.55 |
| unit-leaks (3.14.7) | `unit/test_attempt_tmpdir_leaks.py` | 03:38:26 | 22.16/21.22/21.78 | 197P | 23.75 |
| unit-lifecycle (3.14.7) | `unit/test_attempt_tmpdir_lifecycle.py` | 03:38:56 | 22.54/21.43/21.84 | 34P, 1F | 11.09 |
| unit-mutations (3.14.7) | `unit/test_attempt_tmpdir_mutations.py` | 03:40:43 | 17.96/20.26/21.33 | 85P | 4.59 |
| unit-retention (3.14.7) | `unit/test_attempt_tmpdir_retention.py` | 03:40:49 | 16.93/20.00/21.24 | 6P | 1.37 |
| unit-sandboxes (3.14.7) | `unit/test_attempt_tmpdir_sandboxes.py` | 03:40:58 | 15.89/19.74/21.14 | 12P | 1.81 |
| guard-state (3.14.7) | `fake/test_state_contract.py` | 03:41:05 | 14.14/19.23/20.94 | 37P | 29.07 |
| guard-names (3.14.7) | `unit/test_test_module_names.py` | 03:41:36 | 16.88/19.25/20.88 | 1P | 1.51 |
| guard-invariants (3.14.7) | `unit/test_invariants_index.py` | 03:41:40 | 16.64/19.16/20.84 | 25P | 1.04 |
| unit-lifecycle-retry (3.14.7) | `unit/test_attempt_tmpdir_lifecycle.py` | 03:40:34 | 20.34/20.78/21.53 | 1F | 3.11 |
| lifecycle-control (3.14.7) | `test_lifecycle_counterexample.py` | 03:40:38 | 20.34/20.78/21.53 | 2F | 1.50 |

Both red runs at the starting production source have exactly eight failures.
R2 is green 8/8 on both Pythons with no expected-failure marks. Q passes 47/47
on both. W passes 20/20 five times on 3.14 and once on 3.12. C passes 48/48 on
both Pythons at the final test head (the earlier 46/47-case versions are retained
in the table). The final maintained set contains **766 distinct passing cases and one pre-existing failing lifecycle property**.

The 33 walk plans (11 close plans, 11 throw plans, 11 too-deep plans) and 33
repository plans (11 plans in three .git layouts) verify ownership and the
new precedence on both Pythons. Repository plans and 400 walk + 400 repository
differentials passed in each exploratory parity hold; walk differentials
include 230 raising cases per 400. After the two scratch assertion corrections,
all 33 walk plans pass on each Python. No production change was needed.
For the executed differentials, single-failure behavior without a cleanup
failure matches d3a48269a, including yields, return values, exception class,
TreeError reason, errno and message. This scopes parity to those inputs;
review r3's four counterexamples are additional behavior beyond that matrix.
Baselines were regenerated with the review's git-show instructions in scratch.
The reviewer's in-memory fix was read as a reference, not applied as a patch.

Archive A1/A2 partition functions before line 858 and all remaining functions,
with `-m 'not real_lsof'` in both. The live case
`test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` is explicitly deselected.
The sweep property runs separately from examples, with explicit and generated
phases in separate holds. Its 15 explicit examples and all 40 generated examples
remain intact; no test bound or example count was loosened.

## Unchanged lifecycle property failure

The lifecycle file passes 34 other cases, but
`test_generated_attempt_lifetimes_preserve_isolation_and_cleanup_authority`
fails with the minimized sequence
`start(0), swap(0), vanish(0)`. The model's vanish operation itself removes
its swapped replacement; the subsequent loop reads the victim file and raises
FileNotFoundError. The first run and one rerun are recorded above. A direct
replay of the same input fails both with current retention_fs and with the
starting b20cf84ad source loaded in memory. The lifecycle test and its production
code are unchanged from the starting checkout. This is not a changed cleanup
expectation; it is retained and reported rather than changing unrelated scope.
The control probe source is included in the evidence JSON.

## Mutants

Each opt-in mutation modifies only the imported module in memory. M1 and MI
were adapted to the new ownership guards and helper calls, preserving their
original removed behavior. Every mutant was killed by assertions in its named
tests; there were no plugin/configuration failures. All runs use Python 3.14.7.

| Mutant | Start UTC | Load | P/F | Named killing test |
| --- | --- | --- | --- | --- |
| M1: Drop walk child cleanup | 03:19:29 | 19.45/24.27/25.17 | 4F, 16P | `W::test_hub_low_soft_limit_reproduction[1]` |
| M2: Restore release source | 03:19:33 | 19.89/24.28/25.17 | 11F, 9P | `W::test_hub_low_soft_limit_reproduction[1]` |
| MO: Restore release validation | 03:19:38 | 22.14/24.67/25.30 | 2F | `W::test_open_dir_failed_validation_closes_descriptor[error0]` |
| ML: Restore release allocation | 03:19:43 | 23.73/24.96/25.40 | 1F | `Q::test_layout_allocation_before_open` |
| MI: Drop registration protection | 03:19:50 | 22.07/24.60/25.27 | 1F | `Q::test_in_repository_registration_failure_closes_descriptor` |
| MP: Restore release path descent | 03:20:02 | 20.36/24.15/25.10 | 2F | `W::test_open_path_failed_descent_closes_intermediate_descriptors[a/missing]` |
| MC: Restore release conflict descent | 03:20:10 | 19.80/23.90/25.00 | 3F, 1P | `W::test_conflict_dir_failed_descent_closes_descriptor[error0-mkdir]` |
| MH: Drop handoff protection | 03:20:16 | 20.94/24.07/25.05 | 6F | `Q::test_descent_close_failure_owns_new_child[True-set-aside]` |
| MD: Stop drain at first close error | 03:20:21 | 20.06/23.83/24.96 | 3F | `Q::test_walk_cleanup_interruption_closes_remaining_children[close]` |
| MR: Retry the previous close | 03:20:24 | 20.30/23.82/24.95 | 2F | `R2::test_failed_handoff_close_never_closes_a_reused_number[keyboard-path]` |
| MA: Close regardless of ownership | 03:20:28 | 20.60/23.82/24.94 | 2F | `R2::test_interrupt_after_registration_closes_child_once[walk]` |
| ME: Close error replaces active exception | 03:20:34 | 21.03/23.86/24.95 | 1F | `C::test_open_dir_swapped_identity_survives_close_error` |
| MX: Drop cleanup cancellation | 03:20:41 | 22.55/24.12/25.04 | 1F | `C::test_set_aside_cleanup_cancellation_overrides_handoff_oserror` |

MR runs the two path reuse cases; MA runs both registration sites. ME runs
swapped-identity validation with a failing close; MX runs the handoff OSError
followed by cleanup KeyboardInterrupt through `_set_aside_fd`. The nine
inherited selections retain their prior scope (M1/M2 full W; MO/MP both cases;
MC all four; MH six; MD both walk cases plus repository cleanup; ML/MI one each).

## PR-body replacements for the hub

- `walk` lists each child before the stack owns it, closes it on any exception raised before the stack holds it, and re-raises. An interrupt immediately after append leaves cleanup to the stack.
- Validation, allocation, registration, and failed descent are protected. In `_open_path` and `_conflict_dir`, the new child takes ownership before the previous descriptor is closed; each descriptor receives at most one close attempt, so a failed parent close cleans up the child without retrying a number another thread may have reused.
- Cleanup attempts every remaining owned close once. `walk` preserves the borrowed `root_fd`; the exit behavior follows the invariant below.

**Invariant (achieved for fix r3's tested paths; gaps remained at the historical r2 head):** With nothing in flight, try every owned close once, then raise the first close error. An exception in flight propagates unchanged, except cleanup cancellation overrides an `Exception` or `GeneratorExit`, with that exception as its `__context__`. An in-flight cancellation always wins. Cancellation is a `BaseException` other than `Exception` or `GeneratorExit`, including `KeyboardInterrupt`, `SystemExit`, and custom subclasses.

**Review r2 and fix round r2:** Review r2 (Claude Opus 5.5, lane job `20261029-155526-pr279-review-r2`) requested changes at `a98ee557a` for P2-3 and P3-1–P3-4; regression commit `9629969c4` supplied eight strict expected failures. Fix r2 removes the close retry, respects registration ownership, centralizes explicit error and finally cleanup in `retention_fs.py`, and corrects the Python 3.12 tracing explanation. All eight fail at `b20cf84ad` and pass without marks on Python 3.14.7 and 3.12.14; 766 distinct maintained cases pass, and all thirteen named mutants are killed. One unchanged lifecycle property fails for `start → swap → vanish` at both base and head: its model removes its own replacement, then asserts it still exists. The 33 walk and 33 repository plans and 400-case differentials run on both Pythons, with the intentional GeneratorExit precedence change recorded.

## Delivery and exclusions

Shared Git metadata refused index.lock creation. All coherent steps are
committed on `fix/walk-fd-leak-217` in isolated metadata using read-only base
objects. Only named files were staged, with the required coauthor trailer on
every commit. The shared worktree HEAD stays at b20cf84ad. Delivery is the
verified incremental bundle `docs/reports/2026-10-29-walk-descriptors-r2.bundle`,
from b20cf84ad, whose final head is supplied in the final answer. No history
was rewritten; no fetch, merge, push, or GitHub/network read was performed
apart from environment sync. Fixture Git operations involve only scratch trees.

The requested Darwin scratch root was sandbox-denied. Environments and isolated
metadata used `/private/tmp/sf-fix279r2`; test trees used the permitted per-job
Darwin temp directory's `sf-fix279r2` child so archive fixtures do not inherit
the special literal `tmp` path component. The largest recorded combined scratch sample was 75 MiB, below 500 MB;
successful hold roots were removed after each run. Both roots are removed using
the lane's guarded cleanup after bundle verification. All task-started processes
have ended. No live store, live daemon, real CLI, or real provider was accessed.

Not run: the deselected live lsof case, the full suite, the live frontend
`tests/frontend/test_core_live.py` (lane person-only check; CI owns it), and the
broader regression set on Python 3.12 (it ran on 3.14). No CI job log or current
CI snapshot was requested or read. Every requested non-live case was run; the unchanged lifecycle property failure is documented above.


## Review r3 and fix round r3

Review r3 requested changes at `03d9270b2` for P2-4, P2-5, P3-5 and P3-6.
The r2 cleanup helper inferred an operation's pending exit from ambient state;
handled caller exceptions exposed that mistake. `size` still used a plain root
close, a popped child could become unowned, and `record_complete` implicitly
finalized its walk. Nine strict regression cases fail on both Pythons, with
one passing control, at the merged r3 starting head.

Fix r3 makes pending exits explicit, applies the same owner to `size`, protects
the pop transition, and explicitly closes all ten production walk iterators.
The ten r3 cases pass without marks on both Pythons. The invariant is achieved
for the tested scenarios; universal cleanup claims go only as far as those tests.
The [r3 report](2026-10-29-walk-descriptors-r3.md) names the marker-to-call
instruction gap, reports current runs and all seventeen mutants, and supplies
current PR-body replacements. Historical r2 counts above are unchanged.
