# D-WALK-FD1: fix round r3 and execution evidence

Base: `0a433cfe101ff1edfb2aeac3c28eaa414c8119a0`. Work began 2026-10-30 at 14:09:53 UTC.
Production/test head before the text commit: `8b1742c2d6f9041814918f8f72d330add26fec6b`. Shared Git metadata
rejected staging; the commits are on `fix/walk-fd-leak-217` in isolated metadata.
The verified delivery bundle is `docs/reports/2026-10-29-walk-descriptors-r3.bundle`.
Its final head is supplied in the final answer; the shared checkout HEAD remains
at the base. No history was rewritten, and no fetch, merge or push occurred.

## Decisions built

1. **P2-4 (`13f195412`):** `_close_all` takes the operation's exit exception
   explicitly, with `None` on normal exit. One `_cleanup` context gets that
   exception through `__exit__`; all 27 former single-descriptor finally blocks
   and both drains use it. Handlers
   pass their caught exception directly. Neither cleanup module reads
   `sys.exception()` or `sys.exc_info()`. The three handled-caller regressions
   now raise their own close failure or cancellation, preserving identity and context.
2. **P2-5 (`4d5fb18c7`):** `AttemptTemp.size` uses the same root owner. Its
   existing caught errors still yield `None` on otherwise normal operation;
   an in-flight walk cancellation leaves as the identical object after root cleanup.
3. **P3-5 (`637de852d`):** the focused control was extended through close
   setup. The handler checks whether the popped child remains on the stack,
   and a marker prevents retry after the close attempt begins. The after-pop
   regression and the bounded instruction tests pass. The marker-to-call gap
   below remains outside the guarantee, as the hub's pure-Python exception permits.
4. **P3-6 (`25869da24`):** all ten walk consumers own and explicitly close
   their iterators on every body exit. Enclosing root cleanup in archive,
   survey and git consumers also uses the same precedence, so it preserves
   iterator cleanup cancellation. `record_complete`'s early `False` return
   propagates walk cleanup cancellation with no `sys.unraisablehook` event.
5. **Tests/mutants:** the new bounded Hypothesis property generates operation
   exits, handled caller exceptions and one-to-five close outcomes. It checks
   real scratch descriptors, one attempt per descriptor, exception identity,
   context and cause. Both Pythons run 150 generated examples plus eight explicit
   examples. Source guards cover both cleanup modules and getter aliases.
   Four production mutants supplement the thirteen inherited mutants.
6. **Text:** the main, r1 and r2 reports retain historical run counts but scope
   the old helper/parity claims and acknowledge the r3 counterexamples. The
   invariant is achieved for the exercised scenarios at this head.

No exception-precedence decision changed. The required scratch location was
sandbox-denied; the permitted attempt temp directory's `sf-fix279r3` child was
used instead. The pure-Python instruction boundary is an explicit coverage
qualification, not a claim of universal ownership under asynchronous injection.
The [main invariant](2026-10-29-walk-descriptors.md#invariant) defines precedence.

## Walk-consumer audit

All sites now use the same iterator owner, inside the root's ownership. A
`continue` consumes more entries; an exception or cancellation can leave any
of these bodies before exhaustion.

| Consumer | Early exit and current ownership |
| --- | --- |
| `retention_fs.tree_bytes` | Sum predicate/body failure or cancellation; closes iterator before root. |
| `retention_fs.record_complete` | Two early `False` returns and validation/read failures; explicitly closes its borrowed-root walk. |
| `AttemptTemp.size` | Sum predicate/body failure or cancellation; closes iterator before root and retains its caught-error policy. |
| `retention_git.admin_objects` | Read, close and token-processing failures; closes iterator before root. |
| `Retirement._final_check` | `Defer`, verification and body failures; closes iterator before root. |
| `Retirement._late_admin`, first walk | Verification/read/accounting failures; closes iterator before root. |
| `Retirement._late_admin`, present-set walk | Set allocation failure or cancellation; explicitly closes iterator before root. |
| `_Builder._walk` | Tick/classification/digest/archive failures; closes iterator before root. |
| `retention_survey._walk_sizes` | Accounting or `_entry_bytes` failure; closes iterator before root. |
| `retention_survey._sample_walk_once` | Classification, `NotRegenerable`, hash/accounting failures; closes iterator before root. |

## Red, green, core and parity runs

All times are 2026-10-30 UTC. P/F mean passed/failed. Loads use `os.getloadavg`
immediately after admission, because the wrapper's `sysctl` read was denied.
There is one serial pytest per shared-lock hold, at most two active pytests,
with no run outside the lock. Python 3.14.7 and 3.12.14 use separate locked dev
venvs. Every run clears `SUBFLEET_RETENTION_DORMANT` and `PYTHONPATH`, disables
bytecode and pytest cache, and uses task-owned TMPDIR/Hypothesis storage.
The evidence runner redirects failed_keep into scratch before conftest pruning.
Two auxiliary agents assisted with source audits and new test drafting; test
holds were started only by the root runner. This departs from the host file's
no-subagent instruction under this session's required collaboration mode.

| Hold / Python | File or selection | Start UTC | Load 1/5/15 | Result | Seconds |
| --- | --- | --- | --- | --- | ---: |
| red314 / 3.14.7 | `unit/test_retention_descriptor_review_r3.py` | 14:11:38 | 13.74/9.77/8.38 | 1P, 9F | 4.09 |
| red312 / 3.12.14 | `unit/test_retention_descriptor_review_r3.py` | 14:11:47 | 14.79/10.14/8.53 | 1P, 9F | 2.63 |
| fix-ambient / 3.14.7 | `unit/test_retention_descriptor_review_r3.py` | 14:14:06 | 10.95/10.45/8.90 | 4P | 5.62 |
| fix-cleanup / 3.14.7 | `unit/test_retention_descriptor_cleanup.py` | 14:14:29 | 13.71/11.21/9.22 | 48P | 6.68 |
| fix-size / 3.14.7 | `unit/test_retention_descriptor_review_r3.py` | 14:15:18 | 21.28/14.02/10.40 | 2P | 11.57 |
| fix-pop / 3.14.7 | `unit/test_retention_descriptor_review_r3.py` | 14:16:00 | 36.60/18.17/12.05 | 1P | 6.77 |
| green314 / 3.14.7 | `unit/test_retention_descriptor_review_r3.py` | 14:19:55 | 29.15/22.95/15.50 | 10P | 4.43 |
| green312 / 3.12.14 | `unit/test_retention_descriptor_review_r3.py` | 14:20:53 | 21.07/22.11/15.71 | 10P | 2.56 |
| review314 / 3.14.7 | `unit/test_retention_descriptor_review.py` | 14:20:04 | 30.02/23.33/15.72 | 47P | 5.91 |
| r2314 / 3.14.7 | `unit/test_retention_descriptor_review_r2.py` | 14:20:12 | 30.91/23.74/15.95 | 8P | 7.07 |
| cleanup314 / 3.14.7 | `unit/test_retention_descriptor_cleanup.py` | 14:20:24 | 28.05/23.37/15.91 | 48P | 8.11 |
| ownership314 / 3.14.7 | `unit/test_retention_descriptor_ownership.py` | 14:20:35 | 24.66/22.80/15.80 | 16P | 7.56 |
| walk314-1 / 3.14.7 | `unit/test_retention_walk_descriptors.py` | 14:20:45 | 22.95/22.49/15.77 | 20P | 6.48 |
| walk314-2 / 3.14.7 | `unit/test_retention_walk_descriptors.py` | 14:23:42 | 18.69/20.73/16.29 | 20P | 7.25 |
| walk314-3 / 3.14.7 | `unit/test_retention_walk_descriptors.py` | 14:38:43 | 18.56/18.93/18.34 | 20P | 11.36 |
| walk314-4 / 3.14.7 | `unit/test_retention_walk_descriptors.py` | 14:39:00 | 20.27/19.29/18.48 | 20P | 12.71 |
| walk314-5 / 3.14.7 | `unit/test_retention_walk_descriptors.py` | 14:41:06 | 19.76/19.37/18.62 | 20P | 13.18 |
| parity314 / 3.14.7 | `test_parity.py` | 14:53:59 | 18.82/19.72/19.23 | 4P | 16.60 |
| review312 / 3.12.14 | `unit/test_retention_descriptor_review.py` | 14:57:44 | 19.68/21.68/20.32 | 47P | 11.61 |
| r2312 / 3.12.14 | `unit/test_retention_descriptor_review_r2.py` | 15:11:58 | 32.03/31.73/24.34 | 8P | 7.09 |
| cleanup312 / 3.12.14 | `unit/test_retention_descriptor_cleanup.py` | 15:12:23 | 32.20/31.80/24.58 | 48P | 5.02 |
| ownership312 / 3.12.14 | `unit/test_retention_descriptor_ownership.py` | 15:14:17 | 49.47/37.92/27.84 | 15P | 14.29 |
| walk312-1 / 3.12.14 | `unit/test_retention_walk_descriptors.py` | 15:14:47 | 36.62/35.97/27.53 | 20P | 6.47 |
| parity312 / 3.12.14 | `test_parity.py` | 15:21:05 | 25.44/29.18/27.43 | 4P | 7.51 |

The red runs use unchanged `0a433cfe1` production/test source and `--runxfail`:
exactly nine fail and one passes on each Python. Green is ten passes without
marks on each. The review r3 file differs from base only by removed strict
expected-failure marks. Existing cleanup tests change only two `_close_all`
invocations to pass the explicit pending argument; no existing expectation changed.
The ownership file's count differs across Pythons because their pop-transfer
instruction counts differ; the generated property and source guards are identical.

The unchanged r2 parity harness passes **33 walk plans, 33 repository plans,
400 walk differentials and 400 repository differentials on each Python**. Only
scratch import paths were relocated; plans, generated inputs and expectations
were not edited. Baselines were regenerated by the reviewed git-show commands.
The existing intentional GeneratorExit/close-OSError precedence change remains
accounted for in those plans. This round adds four named changes beyond that
parity domain: handled caller cleanup errors propagate; `size` root cleanup
preserves the original walk cancellation; the tested popped child closes; and
explicit iterator cleanup cancellation propagates through early consumer exits.
Parity claims cover the executed differentials, not every possible failure.

## Regression set and guards

Archive A1/A2 use the unchanged reviewed partition and both deselect
`test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` with `-m 'not real_lsof'`.
The sweep examples, all 15 explicit examples and all 40 generated examples run
in separate holds. No application/test bound or example count was loosened.
These regression and guard files run on Python 3.14.7. The maintained set on
3.14 contains 793 passing cases and zero failures, counting the sweep property once across its two phase holds and excluding intentional red/mutant failures.

| Hold / Python | File or selection | Start UTC | Load 1/5/15 | Result | Seconds |
| --- | --- | --- | --- | --- | ---: |
| archive1 / 3.14.7 | `unit/test_retention_archive.py` | 14:23:52 | 21.45/21.27/16.53 | 38P | 210.36 |
| archive2 / 3.14.7 | `unit/test_retention_archive.py` | 14:27:27 | 21.20/21.37/17.61 | 70P | 241.10 |
| sweep-examples / 3.14.7 | `unit/test_retention_sweep_race.py` | 14:31:32 | 19.46/21.47/18.62 | 12P | 46.84 |
| sweep-explicit / 3.14.7 | `unit/test_retention_sweep_race.py` | 14:32:24 | 17.40/20.63/18.47 | 1P | 68.64 |
| sweep-generated / 3.14.7 | `unit/test_retention_sweep_race.py` | 14:33:42 | 15.85/19.54/18.26 | 1P | 241.15 |
| turns / 3.14.7 | `unit/test_retention_turns.py` | 14:39:00 | 20.27/19.29/18.48 | 7P | 14.53 |
| readback / 3.14.7 | `unit/test_retention_readback.py` | 14:39:56 | 20.74/19.69/18.68 | 37P | 316.25 |
| regenerable / 3.14.7 | `unit/test_retention_regenerable.py` | 14:52:31 | 19.87/19.92/19.24 | 26P | 86.00 |
| regenerable-properties / 3.14.7 | `unit/test_retention_regenerable_properties.py` | 14:54:39 | 19.05/19.75/19.27 | 1P | 139.04 |
| timers / 3.14.7 | `unit/test_timers_retention.py` | 14:58:00 | 17.89/21.19/20.17 | 20P | 25.17 |
| fake-daemon / 3.14.7 | `fake/test_attempt_tmpdir_daemon.py` | 14:58:51 | 13.44/19.47/19.59 | 24P | 39.03 |
| fake-leaks / 3.14.7 | `fake/test_attempt_tmpdir_leaks_daemon.py` | 15:05:20 | 14.46/14.34/16.89 | 8P | 21.47 |
| unit-guardian / 3.14.7 | `unit/test_attempt_tmpdir_guardian.py` | 15:12:09 | 32.84/31.91/24.49 | 2P | 6.02 |
| unit-leaks / 3.14.7 | `unit/test_attempt_tmpdir_leaks.py` | 15:12:42 | 36.85/32.87/25.13 | 197P | 80.38 |
| unit-lifecycle / 3.14.7 | `unit/test_attempt_tmpdir_lifecycle.py` | 15:21:18 | 23.41/28.55/27.23 | 35P | 11.10 |
| unit-mutations / 3.14.7 | `unit/test_attempt_tmpdir_mutations.py` | 15:24:38 | 31.19/28.89/27.55 | 85P | 8.00 |
| unit-retention / 3.14.7 | `unit/test_attempt_tmpdir_retention.py` | 15:25:00 | 29.90/28.74/27.52 | 6P | 3.26 |
| unit-sandboxes / 3.14.7 | `unit/test_attempt_tmpdir_sandboxes.py` | 15:25:12 | 30.05/28.84/27.58 | 12P | 5.13 |
| state / 3.14.7 | `fake/test_state_contract.py` | 15:25:35 | 27.35/28.30/27.42 | 37P | 31.71 |
| module-names / 3.14.7 | `unit/test_test_module_names.py` | 15:26:20 | 28.20/28.36/27.48 | 1P | 2.45 |
| invariants / 3.14.7 | `unit/test_invariants_index.py` | 15:26:29 | 27.34/28.16/27.42 | 25P | 1.57 |

The lifecycle file passed all 35 generated cases in this run. The known directed `start → swap → vanish` model defect remains outside this round; neither its test nor model was changed.

## Mutants

All seventeen mutants apply to imported production code in memory, each against
its named tests. No production source file is changed by a mutant. A kill means
a call-phase assertion failed in the named test; plugin/configuration errors do
not count. M1, MI and MA were retargeted to explicit exception arguments and the
new indentation. MD now accepts `pending` and the resource-close callback so it
still removes drain behavior rather than failing on the changed API. The
remaining inherited mutations retain their prior behavior and selections.
MAH reintroduces ambient inference in `_close_all`; MSR restores only `size`'s
plain root close; MPO removes the pop handler; MGF removes `record_complete`'s
iterator owner. The named killers below all fail.

| Mutant | Start UTC | Load 1/5/15 | P/F | Named killing test |
| --- | --- | --- | --- | --- |
| M1 | 14:23:36 | 18.49/20.73/16.26 | 16P, 4F | `tests/unit/test_retention_walk_descriptors.py::test_hub_low_soft_limit_reproduction[1]` |
| M2 | 14:37:51 | 19.25/19.06/18.35 | 9P, 11F | `tests/unit/test_retention_walk_descriptors.py::test_hub_low_soft_limit_reproduction[1]` |
| MO | 14:38:23 | 18.40/18.92/18.33 | 2F | `tests/unit/test_retention_walk_descriptors.py::test_open_dir_failed_validation_closes_descriptor[error0]` |
| ML | 14:45:19 | 16.53/17.31/17.81 | 1F | `tests/unit/test_retention_descriptor_review.py::test_layout_allocation_before_open` |
| MI | 14:54:24 | 20.13/19.99/19.35 | 1F | `tests/unit/test_retention_descriptor_review.py::test_in_repository_registration_failure_closes_descriptor` |
| MP | 14:57:21 | 24.20/22.61/20.60 | 2F | `tests/unit/test_retention_walk_descriptors.py::test_open_path_failed_descent_closes_intermediate_descriptors[a/missing]` |
| MC | 14:58:40 | 14.62/19.90/19.74 | 1P, 3F | `tests/unit/test_retention_walk_descriptors.py::test_conflict_dir_failed_descent_closes_descriptor[error0-mkdir]` |
| MH | 15:05:00 | 12.12/13.94/16.82 | 6F | `tests/unit/test_retention_descriptor_review.py::test_descent_close_failure_owns_new_child[True-set-aside]` |
| MD | 15:12:30 | 33.22/32.02/24.70 | 3F | `tests/unit/test_retention_descriptor_review.py::test_walk_cleanup_interruption_closes_remaining_children[close]` |
| MR | 15:14:04 | 52.76/38.13/27.79 | 2F | `tests/unit/test_retention_descriptor_review_r2.py::test_failed_handoff_close_never_closes_a_reused_number[keyboard-path]` |
| MA | 15:14:37 | 40.93/36.73/27.64 | 2F | `tests/unit/test_retention_descriptor_review_r2.py::test_interrupt_after_registration_closes_child_once[walk]` |
| ME | 15:14:56 | 37.46/36.16/27.64 | 1F | `tests/unit/test_retention_descriptor_cleanup.py::test_open_dir_swapped_identity_survives_close_error` |
| MX | 15:24:49 | 30.54/28.81/27.54 | 1F | `tests/unit/test_retention_descriptor_cleanup.py::test_set_aside_cleanup_cancellation_overrides_handoff_oserror` |
| MAH | 15:25:06 | 30.87/28.96/27.61 | 1P, 3F | `tests/unit/test_retention_descriptor_review_r3.py::test_normal_read_close_failure_is_not_suppressed_by_handled_caller_exception[keyboard-keyboard]` |
| MSR | 15:25:25 | 28.90/28.63/27.52 | 2F | `tests/unit/test_retention_descriptor_review_r3.py::test_attempt_temp_size_root_close_preserves_walk_cancellation[os]` |
| MPO | 15:26:10 | 27.12/28.14/27.39 | 1F | `tests/unit/test_retention_descriptor_review_r3.py::test_interrupt_after_stack_pop_closes_popped_child` |
| MGF | 15:26:26 | 27.63/28.24/27.44 | 3F | `tests/unit/test_retention_descriptor_review_r3.py::test_record_complete_early_return_propagates_cleanup_cancellation[keyboard]` |

## Coverage boundaries

The universal phrase “every cleanup” goes only as far as these tests. The ten
review r3 cases, generated property and bounded instruction tests pass, but do
not establish behavior at every Python or native instruction boundary.

- On Python 3.14, pop-return-through-marker offsets are
  `[320, 322, 324, 326, 330, 334, 336, 338]`, and close return is
  `382`. The untested marker-executed-to-close
  window is `[340, 350, 370, 372, 374]`.
- On Python 3.12, the corresponding tested offsets are
  `[278, 280, 282, 284, 288, 290, 292]` and `334`;
  the untested window is `[294, 304, 324, 326]`.
- Pure Python cannot atomically mark an attempt and execute native `close`.
  An interrupt in that window can leave the popped child unclosed. Native
  `close` interruption also leaves syscall completion ambiguous; no close is retried.
- Helper construction/entry after acquisition, unexercised open-return-to-binding
  or registration instructions, normal `with`-exit setup,
  failures while constructing cleanup arguments, repeated interrupts in a
  cleanup handler and native syscall interruption are untested. Allocation
  failure before the helper is entered can leave the acquired resource unowned.
- `_Builder._walk` and `_sample_walk_once` already construct `RegenerableWalk`
  before root ownership; failure there remains an uncovered acquisition boundary.
- Generated close hooks release their real descriptor before raising. These
  tests do not prove that an arbitrary failing native close released its number.

## PR-body lines for the hub

- `walk` lists each child before registering it, checks registration ownership,
  and protects the tested pop transition. Failed validation and descent cleanup
  retain one close attempt per owned descriptor; the borrowed root stays open.
- Cleanup receives the operation's explicit exit exception, including
  `AttemptTemp.size`'s root close. Handled caller exceptions do not suppress
  this operation's cleanup failure or cancellation.
- All ten production `walk` consumers explicitly close their iterators on every
  body exit, so walk cleanup cancellation propagates through early returns.

**Parity line:** The named single-failure differentials retain release exception
types and `TreeError` codes for their executed inputs. The broader cleanup rule
has the tested scope and instruction-boundary qualifications in the r3 report.

**Invariant:** Each owned descriptor receives at most one close attempt; the
borrowed root stays open. With nothing in flight, attempt every owned close,
then raise the first close error. An in-flight exception propagates unchanged,
except cleanup cancellation overrides an `Exception` or `GeneratorExit` with
that exception as its `__context__`. An in-flight cancellation always wins.
Cancellation is a `BaseException` other than `Exception` or `GeneratorExit`.
The invariant is achieved in the exercised scenarios; universal coverage extends
only as far as the tests, with the pop marker-to-call and other instruction
boundaries recorded in the r3 report.

**Fix round r3:** Review r3 requested changes at `03d9270b2` for P2-4, P2-5,
P3-5 and P3-6. At merged base `0a433cfe1`, nine cases fail and one passes on
both Pythons. Fix r3 uses explicit operation exits, the same root owner in
`size`, protected pop ownership and explicit walk-iterator closure. All ten
cases now pass without marks on Python 3.14.7 and 3.12.14. The generated
precedence property, unchanged 33+33 plans and 400+400 differentials pass on
both Pythons, and all seventeen named mutants are killed. The r3 report scopes
these claims to tested paths and instruction windows. D-LIFECYCLE-MODEL1 remains
with the hub and is unchanged by this round.

## Delivery, process and disk checks

Every coherent step is committed with the required coauthor trailer; only named
files are staged. The bundle is incremental from `0a433cfe101ff1edfb2aeac3c28eaa414c8119a0` and is verified
before scratch cleanup. The sandbox-required scratch root is `/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261030-100933-pr279-fix-r3-a1-2v00csq1/sf-fix279r3`; it avoids
the special literal `tmp` component in walked fixture paths. Scratch samples
remain below 500 MB; the sample at evidence collection is 145.12 MiB.
The largest sampled scratch size is 145.12 MiB.
The longest completed pytest is 316.25s;
peak runner RSS is 117.48 MiB.
All 62 holds have completed; their start timestamps are recorded above.
The first hold started at 14:11:38 UTC; the core, regression and mutant batch drivers were all started within the first hour. Shared-lock waits pushed later individual admissions past that hour. Those late starts are recorded explicitly rather than claimed on time.
No run was detached. Every task-started process is ended before the final answer,
and scratch is removed with the rules file's guarded cleanup after verification.
No live store/process inspection, real CLI, real provider, or caller checkout
write occurred. No network beyond environment sync was used.

Not run: the deselected live `lsof` case, the full suite, the live frontend file
left to CI, broader regression files on Python 3.12, or CI status/job logs.
Every requested non-live file/selection was exercised. Full arguments, sources,
logs, exception details and file hashes are in the
[r3 fix evidence JSON](2026-10-29-walk-descriptors-r3-fix-evidence.json).
